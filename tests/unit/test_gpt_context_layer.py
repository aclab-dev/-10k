"""Tests del GPT Context Layer — integración GPTClient + TokenBudgetManager + PromptBuilder.

Cubre los escenarios del DoD de la tarjeta [66]:
  - Respuesta válida
  - JSON inválido
  - Schema fail
  - Timeout
  - Rate limit
  - Token budget excedido (hard block y soft block)

El budget_manager se mockea a nivel de MagicMock para aislar la lógica
del GPTClient sin requerir DB real.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.core.config import Environment, FailurePolicyConfig
from backend.decision_engine.gpt_client import (
    GPTClient,
    GPTClientConfig,
    GPTClientError,
    GPTRateLimitError,
    GPTRequest,
    GPTResponseValidationError,
    RequestPurpose,
)
from backend.decision_engine.prompt_builder import (
    PROMPT_VERSION,
    AccountContext,
    PromptBuilder,
    PromptContext,
)
from backend.decision_engine.schemas import DecisionType, ModelDecision
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.schemas import (
    FundingState,
    LiquidityState,
    MarketRegimeAssessment,
    OpenInterestState,
    PrimaryRegime,
    TrendAlignment,
    VolatilityState,
)
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.token_budget.manager import TokenBudgetExceededError
from backend.token_budget.schemas import BudgetCheckResult, BudgetStatus
from backend.volatility.schemas import VolatilityAssessmentPackage, VolatilityRegime

# ---------------------------------------------------------------------------
# Constantes de test
# ---------------------------------------------------------------------------

_TEST_API_KEY = "sk-test-not-real"

_POLICY_DEFAULT = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=True,
    token_budget_failure_blocks_new_entries=True,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)

_POLICY_PERMISSIVE = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=False,
    token_budget_failure_blocks_new_entries=False,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)

# ---------------------------------------------------------------------------
# Helpers de fixtures de mercado (reutilizados del estilo test_prompt_builder)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(price: str = "50000") -> CandleData:
    p = Decimal(price)
    return CandleData(
        open=p,
        high=p + 100,
        low=p - 100,
        close=p + 5,
        volume=Decimal("1000"),
        n_candles=1,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_utc=_now(),
        exchange=Exchange.BINGX,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=Decimal("50005"),
        bid=Decimal("50000"),
        ask=Decimal("50010"),
        spread_absolute=Decimal("10"),
        spread_percent=Decimal("0.02"),
        candles=Candles(
            tf_5m=_candle("50000"),
            tf_15m=_candle("50100"),
            tf_1h=_candle("49800"),
            tf_4h=_candle("49000"),
        ),
        volume=Decimal("5000"),
        account_balance_usdt=Decimal("100"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=_now(),
        local_time=_now(),
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _quant_signals() -> QuantSignalsPackage:
    return QuantSignalsPackage(
        snapshot_id=str(uuid.uuid4()),
        timestamp_utc=_now(),
        symbol="BTCUSDT",
        timeframes_used=["5m", "15m", "1h", "4h"],
        momentum_signal=0.65,
        mean_reversion_signal=-0.20,
        breakout_signal=0.50,
        funding_signal=0.10,
        open_interest_signal=0.30,
        order_flow_imbalance_signal=0.40,
        liquidity_sweep_signal=0.05,
        signal_strength_score=0.72,
        signal_conflict_score=0.25,
        signal_confidence=0.68,
    )


def _regime() -> MarketRegimeAssessment:
    return MarketRegimeAssessment(
        snapshot_id=str(uuid.uuid4()),
        symbol="BTCUSDT",
        timestamp_utc=_now(),
        primary_regime=PrimaryRegime.TRENDING,
        regime_confidence=0.78,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        funding_state=FundingState.NEUTRAL,
        open_interest_state=OpenInterestState.RISING,
        trend_alignment=TrendAlignment.BULLISH,
    )


def _volatility() -> VolatilityAssessmentPackage:
    return VolatilityAssessmentPackage(
        snapshot_id=str(uuid.uuid4()),
        timestamp_utc=_now(),
        symbol="BTCUSDT",
        atr_5m=Decimal("80"),
        atr_15m=Decimal("120"),
        atr_1h=Decimal("200"),
        atr_4h=Decimal("350"),
        atr_percent=0.40,
        realized_vol=0.004,
        volatility_regime=VolatilityRegime.CONTRACTION,
        volatility_score=0.30,
        liquidation_risk_score=0.20,
        leverage_cap=8,
        details={},
    )


def _account() -> AccountContext:
    return AccountContext(
        environment="PAPER",
        balance_usdt=100.0,
        open_positions_count=0,
        daily_drawdown_percent=0.5,
        max_leverage_for_environment=10,
    )


def _prompt_context() -> PromptContext:
    return PromptContext(
        snapshot=_snapshot(),
        quant_signals=_quant_signals(),
        regime=_regime(),
        volatility=_volatility(),
        account=_account(),
    )


# ---------------------------------------------------------------------------
# Helpers de mock HTTP
# ---------------------------------------------------------------------------


def _valid_decision_dict() -> dict[str, Any]:
    return {
        "decision_id": str(uuid.uuid4()),
        "challenge_mode": "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK",
        "schema_version": "1.0",
        "environment": "PAPER",
        "timestamp_utc": "2026-05-25T10:00:00Z",
        "decision": "NO_OPERAR",
        "symbol": "BTCUSDT",
        "market": "USDT_M_FUTURES",
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "entry_type": "NO_ENTRY",
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "invalidation_price": 0.0,
        "leverage": 1,
        "margin_usdt": 0.0,
        "estimated_notional_usdt": 0.0,
        "estimated_entry_fee_usdt": 0.0,
        "estimated_exit_fee_usdt": 0.0,
        "estimated_slippage_usdt": 0.0,
        "estimated_funding_usdt": 0.0,
        "net_risk_reward": 0.0,
        "estimated_max_loss_usdt": 0.0,
        "liquidation_distance_percent_estimated": 0.0,
        "confidence": 0.0,
        "market_regime": "UNCLEAR",
        "setup_name": "no_setup",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "quant_signals": {
            "momentum": "BULLISH",
            "mean_reversion": "LONG_BIAS",
            "breakout_detection": "CONFIRMED",
            "funding_analysis": "SUPPORTS_TRADE",
            "open_interest_analysis": "RISING_WITH_PRICE",
            "order_flow_imbalance": "BUY_PRESSURE",
            "liquidity_sweep": "NONE",
        },
        "decision_aggregator": {
            "quant_score": 0.75,
            "gpt_context_score": 0.70,
            "risk_quality_score": 0.80,
            "final_trade_quality_score": 0.75,
        },
        "news_context": {
            "used": False,
            "impact": "NEUTRAL",
            "summary": "No relevant news.",
        },
        "position_management_plan": {
            "use_trailing_stop": False,
            "move_to_break_even": False,
            "partial_close_plan": "none",
            "max_time_in_trade_minutes": 0,
        },
        "decision_rationale_summary": "No hay edge suficiente.",
        "risk_notes": [],
        "execute": False,
    }


def _openai_http_response(
    content: str,
    prompt_tokens: int = 200,
    completion_tokens: int = 100,
) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.text = content
    mock_resp.headers = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {"content": content, "role": "assistant"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }
    return mock_resp


def _rate_limit_http_response(retry_after: str | None = None) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 429
    mock_resp.text = "Rate limit exceeded"
    mock_resp.headers = MagicMock()
    mock_resp.headers.get.side_effect = lambda k, d=None: (
        retry_after if k == "Retry-After" and retry_after else d
    )
    return mock_resp


def _server_error_http_response(status: int = 500) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status
    mock_resp.text = "Internal Server Error"
    mock_resp.headers = MagicMock()
    mock_resp.headers.get.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# Helpers de budget mock
# ---------------------------------------------------------------------------


def _budget_ok(
    tokens_used_hour: int = 500,
    tokens_used_day: int = 2000,
    status: BudgetStatus = BudgetStatus.NORMAL,
) -> BudgetCheckResult:
    return BudgetCheckResult(
        ok=True,
        status=status,
        tokens_used_hour=tokens_used_hour,
        tokens_used_day=tokens_used_day,
        limit_hour=10_000,
        limit_day=50_000,
    )


def _budget_exceeded() -> BudgetCheckResult:
    return BudgetCheckResult(
        ok=False,
        status=BudgetStatus.EXCEEDED,
        tokens_used_hour=10_000,
        tokens_used_day=50_000,
        limit_hour=10_000,
        limit_day=50_000,
    )


def _make_budget_manager(*, check_result: BudgetCheckResult | None = None) -> MagicMock:
    """Devuelve un mock de TokenBudgetManager con check_budget configurado."""
    manager = MagicMock()
    manager.check_budget.return_value = check_result or _budget_ok()
    return manager


# ---------------------------------------------------------------------------
# Helper para construir GPTClient con budget_manager opcional
# ---------------------------------------------------------------------------


def _make_client(
    *,
    max_retries: int = 0,
    base_delay: float = 0.0,
    failure_policy: FailurePolicyConfig | None = None,
    budget_manager: MagicMock | None = None,
) -> GPTClient:
    config = GPTClientConfig(
        max_retries=max_retries,
        base_delay_seconds=base_delay,
        max_delay_seconds=10.0,
    )
    return GPTClient(
        config=config,
        failure_policy=failure_policy or _POLICY_DEFAULT,
        api_key=_TEST_API_KEY,
        budget_manager=budget_manager,
    )


def _make_request() -> GPTRequest:
    return GPTRequest(
        system_prompt="You are a test evaluator.",
        user_prompt="Evaluate this context.",
        prompt_version=PROMPT_VERSION,
    )


# ===========================================================================
# Tests: respuesta válida con budget manager
# ===========================================================================


class TestValidResponse:
    @pytest.mark.asyncio
    async def test_valid_response_returns_model_decision(self) -> None:
        """Respuesta válida + budget NORMAL → retorna ModelDecision."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)
        assert result.decision == DecisionType.NO_OPERAR

    @pytest.mark.asyncio
    async def test_valid_response_calls_check_budget_once(self) -> None:
        """Budget se verifica exactamente una vez por llamada NEW_ENTRY."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        budget.check_budget.assert_called_once_with(RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_valid_response_records_token_usage(self) -> None:
        """Tras respuesta válida, record_usage recibe los tokens de la respuesta HTTP."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content, prompt_tokens=300, completion_tokens=150),
        ):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        budget.record_usage.assert_called_once()
        call_kwargs = budget.record_usage.call_args.kwargs
        assert call_kwargs["prompt_tokens"] == 300
        assert call_kwargs["completion_tokens"] == 150

    @pytest.mark.asyncio
    async def test_valid_response_symbol_matches_request(self) -> None:
        """ModelDecision devuelto tiene el symbol correcto del dict de respuesta."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        d = _valid_decision_dict()
        d["symbol"] = "ETHUSDT"
        content = json.dumps(d)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)
        assert result.symbol == "ETHUSDT"


# ===========================================================================
# Tests: JSON inválido
# ===========================================================================


class TestInvalidJSON:
    @pytest.mark.asyncio
    async def test_non_json_response_raises_validation_error(self) -> None:
        """GPT devuelve texto plano (no JSON) → GPTResponseValidationError."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        plain_text = "Lo siento, no puedo responder en este momento."

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(plain_text),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_validation_error(self) -> None:
        """JSON malformado (llaves sin cerrar) → GPTResponseValidationError."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        malformed = '{"decision": "NO_OPERAR", "symbol": "BTCUSDT"'  # sin cerrar

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(malformed),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json_raises_validation_error(self) -> None:
        """GPT envuelve el JSON en bloque markdown → GPTResponseValidationError.

        El sistema exige JSON puro; markdown-wrapped no es aceptable.
        """
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        wrapped = f"```json\n{json.dumps(_valid_decision_dict())}\n```"

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(wrapped),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_retry(self) -> None:
        """JSON inválido no es error transitorio — no se reintenta."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=3, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response("not json <<<"),
        ) as mock_post:
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert mock_post.call_count == 1


# ===========================================================================
# Tests: schema fail
# ===========================================================================


class TestSchemaFail:
    @pytest.mark.asyncio
    async def test_json_missing_required_fields_raises_validation_error(self) -> None:
        """JSON válido pero faltan campos requeridos del schema → GPTResponseValidationError."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        incomplete = json.dumps({"decision": "NO_OPERAR", "symbol": "BTCUSDT"})

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(incomplete),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_json_empty_object_raises_validation_error(self) -> None:
        """JSON vacío {} → GPTResponseValidationError (schema requiere campos)."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response("{}"),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_invalid_enum_value_raises_validation_error(self) -> None:
        """decision con valor no permitido → GPTResponseValidationError."""
        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        d = _valid_decision_dict()
        d["decision"] = "MAYBE"  # no es LONG, SHORT ni NO_OPERAR
        content = json.dumps(d)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_schema_fail_does_not_retry(self) -> None:
        """Schema fail no es error transitorio — no se reintenta."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=3, budget_manager=budget)
        bad = json.dumps({"foo": "bar"})

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(bad),
        ) as mock_post:
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert mock_post.call_count == 1

    @pytest.mark.asyncio
    async def test_schema_fail_raises_even_when_policy_permissive(self) -> None:
        """Schema fail siempre levanta, incluso con gpt_failure_blocks_new_entries=False.

        Una respuesta corrupta no puede derivar en un trade — no hay fallback
        seguro para un JSON que no pasa el schema guard.
        """
        budget = _make_budget_manager()
        client = _make_client(failure_policy=_POLICY_PERMISSIVE, budget_manager=budget)
        bad = json.dumps({"totally": "wrong"})

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(bad),
        ):
            with pytest.raises(GPTResponseValidationError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


# ===========================================================================
# Tests: timeout
# ===========================================================================


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_on_all_attempts_raises_gpt_client_error(self) -> None:
        """Timeout en todos los intentos → GPTClientError (NEW_ENTRY, policy blocks)."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=1, base_delay=0.0, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timed out"),
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(GPTClientError):
                    await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_timeout_then_success_returns_model_decision(self) -> None:
        """Timeout en primer intento, éxito en segundo → ModelDecision."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=2, base_delay=0.0, budget_manager=budget)
        content = json.dumps(_valid_decision_dict())
        call_count = 0

        async def _post_with_recovery(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.TimeoutException("timed out")
            return _openai_http_response(content)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=_post_with_recovery,
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_position_management_returns_none(self) -> None:
        """Timeout en POSITION_MANAGEMENT con deterministic=True → None (no raise)."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_new_entry_permissive_policy_returns_none(self) -> None:
        """Timeout en NEW_ENTRY con gpt_failure_blocks_new_entries=False → None."""
        budget = _make_budget_manager()
        client = _make_client(
            max_retries=0, failure_policy=_POLICY_PERMISSIVE, budget_manager=budget
        )

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert result is None


# ===========================================================================
# Tests: rate limit
# ===========================================================================


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_retries_raises(self) -> None:
        """429 en todos los intentos → GPTRateLimitError."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=2, base_delay=0.0, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_rate_limit_http_response(),
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(GPTRateLimitError):
                    await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_rate_limit_then_success_returns_model_decision(self) -> None:
        """429 en primer intento, éxito en segundo → ModelDecision."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=2, base_delay=0.0, budget_manager=budget)
        content = json.dumps(_valid_decision_dict())
        side_effects = [_rate_limit_http_response(), _openai_http_response(content)]

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)

    @pytest.mark.asyncio
    async def test_rate_limit_retry_after_header_respected(self) -> None:
        """Retry-After header en 429 → asyncio.sleep usa ese valor (dentro del cap)."""
        budget = _make_budget_manager()
        # max_delay_seconds=10.0 por defecto; Retry-After=7 queda dentro del cap
        client = _make_client(max_retries=2, base_delay=1.0, budget_manager=budget)
        content = json.dumps(_valid_decision_dict())
        side_effects = [_rate_limit_http_response(retry_after="7"), _openai_http_response(content)]

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        mock_sleep.assert_called_once_with(7.0)

    @pytest.mark.asyncio
    async def test_rate_limit_position_management_returns_none(self) -> None:
        """429 en POSITION_MANAGEMENT con deterministic=True → None (no raise)."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_rate_limit_http_response(),
        ):
            result = await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)

        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_new_entry_permissive_policy_returns_none(self) -> None:
        """429 en NEW_ENTRY con gpt_failure_blocks_new_entries=False → None."""
        budget = _make_budget_manager()
        client = _make_client(
            max_retries=0, failure_policy=_POLICY_PERMISSIVE, budget_manager=budget
        )

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_rate_limit_http_response(),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert result is None


# ===========================================================================
# Tests: errores de servidor (5xx)
# ===========================================================================


class TestServerError:
    @pytest.mark.asyncio
    async def test_server_error_exhausted_retries_raises(self) -> None:
        """500 en todos los intentos → GPTClientError."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=1, base_delay=0.0, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_server_error_http_response(500),
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(GPTClientError):
                    await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    @pytest.mark.asyncio
    async def test_server_error_then_success_returns_model_decision(self) -> None:
        """500 en primer intento, éxito en segundo → ModelDecision."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=2, base_delay=0.0, budget_manager=budget)
        content = json.dumps(_valid_decision_dict())
        side_effects = [_server_error_http_response(500), _openai_http_response(content)]

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)

    @pytest.mark.asyncio
    async def test_server_error_position_management_returns_none(self) -> None:
        """500 en POSITION_MANAGEMENT con deterministic=True → None (no raise)."""
        budget = _make_budget_manager()
        client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT, budget_manager=budget)

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_server_error_http_response(503),
        ):
            result = await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)

        assert result is None

    @pytest.mark.asyncio
    async def test_server_error_new_entry_permissive_policy_returns_none(self) -> None:
        """500 en NEW_ENTRY con gpt_failure_blocks_new_entries=False → None."""
        budget = _make_budget_manager()
        client = _make_client(
            max_retries=0, failure_policy=_POLICY_PERMISSIVE, budget_manager=budget
        )

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_server_error_http_response(502),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert result is None


# ===========================================================================
# Tests: token budget excedido
# ===========================================================================


class TestTokenBudgetExceeded:
    @pytest.mark.asyncio
    async def test_hard_block_raises_token_budget_exceeded_error(self) -> None:
        """Budget agotado + policy blocks → TokenBudgetExceededError se propaga."""
        budget = MagicMock()
        budget.check_budget.side_effect = TokenBudgetExceededError("hourly budget exceeded")
        client = _make_client(budget_manager=budget)

        with patch.object(client._http_client, "post", new_callable=AsyncMock) as mock_post:
            with pytest.raises(TokenBudgetExceededError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        # OpenAI no debe ser llamada si el budget bloquea
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_soft_block_returns_none_without_calling_openai(self) -> None:
        """Budget agotado con ok=False (soft block) → None sin llamar a OpenAI."""
        budget = _make_budget_manager(check_result=_budget_exceeded())
        client = _make_client(budget_manager=budget)

        with patch.object(client._http_client, "post", new_callable=AsyncMock) as mock_post:
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert result is None
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_not_checked_for_position_management(self) -> None:
        """Budget no se verifica para POSITION_MANAGEMENT — siempre pasa."""
        # Aunque check_budget retornaría exceded si se llamara, la lógica
        # del cliente no lo consulta para POSITION_MANAGEMENT.
        budget = MagicMock()
        budget.check_budget.side_effect = TokenBudgetExceededError("should not be raised")
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)

        assert isinstance(result, ModelDecision)
        budget.check_budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_warning_does_not_block(self) -> None:
        """Budget en WARNING pero ok=True → llamada procede normalmente."""
        budget = _make_budget_manager(check_result=_budget_ok(status=BudgetStatus.WARNING))
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)

    @pytest.mark.asyncio
    async def test_no_budget_manager_does_not_check_budget(self) -> None:
        """Sin budget_manager, no se realiza ningún chequeo de budget."""
        client = _make_client(budget_manager=None)
        content = json.dumps(_valid_decision_dict())

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)


# ===========================================================================
# Tests: PromptBuilder como entrada al GPT Context Layer
# ===========================================================================


class TestPromptBuilderIntegration:
    def test_prompt_builder_produces_gpt_request(self) -> None:
        """PromptBuilder genera sistema y usuario válidos para armar un GPTRequest."""
        builder = PromptBuilder()
        ctx = _prompt_context()
        system, user = builder.build(ctx)

        request = GPTRequest(
            system_prompt=system,
            user_prompt=user,
            prompt_version=ctx.prompt_version,
        )

        assert request.system_prompt == system
        assert request.user_prompt == user
        assert request.prompt_version == PROMPT_VERSION

    def test_prompt_includes_symbol(self) -> None:
        """User prompt incluye el symbol del snapshot."""
        builder = PromptBuilder()
        _, user = builder.build(_prompt_context())
        assert "BTCUSDT" in user

    def test_prompt_includes_all_quant_signal_keys(self) -> None:
        """User prompt incluye las señales cuantitativas principales."""
        builder = PromptBuilder()
        _, user = builder.build(_prompt_context())
        for key in ["Momentum", "Mean reversion", "Breakout", "Funding"]:
            assert key in user, f"Sección '{key}' ausente en el user prompt"

    def test_prompt_includes_account_context(self) -> None:
        """User prompt incluye el estado de cuenta: entorno, balance y límite de margen."""
        builder = PromptBuilder()
        _, user = builder.build(_prompt_context())
        assert "PAPER" in user
        # balance_usdt=100.0 → formateado como "100.00 USDT"
        assert "100.00 USDT" in user
        # límite de margen está hardcodeado en el PromptBuilder como restricción de seguridad
        assert "Margen máximo por operación: 10 USDT" in user

    def test_prompt_includes_market_regime(self) -> None:
        """User prompt incluye el régimen de mercado."""
        builder = PromptBuilder()
        _, user = builder.build(_prompt_context())
        assert "TRENDING" in user

    def test_prompt_includes_volatility_atr(self) -> None:
        """User prompt incluye valores de ATR de volatilidad."""
        builder = PromptBuilder()
        _, user = builder.build(_prompt_context())
        assert "ATR" in user

    def test_system_prompt_forbids_direct_execution(self) -> None:
        """System prompt incluye restricción de que GPT no ejecuta órdenes."""
        builder = PromptBuilder()
        system, _ = builder.build(_prompt_context())
        assert "NO puedes ejecutar" in system or "no puede ejecutar" in system.lower()

    def test_system_prompt_includes_schema_version(self) -> None:
        """System prompt incluye la versión del schema del prompt."""
        builder = PromptBuilder()
        system, _ = builder.build(_prompt_context())
        assert PROMPT_VERSION in system

    @pytest.mark.asyncio
    async def test_prompt_builder_output_feeds_gpt_client(self) -> None:
        """El output de PromptBuilder puede usarse directamente con GPTClient."""
        builder = PromptBuilder()
        ctx = _prompt_context()
        system, user = builder.build(ctx)

        budget = _make_budget_manager()
        client = _make_client(budget_manager=budget)
        content = json.dumps(_valid_decision_dict())

        req = GPTRequest(
            system_prompt=system,
            user_prompt=user,
            prompt_version=ctx.prompt_version,
        )

        with patch.object(
            client._http_client,
            "post",
            new_callable=AsyncMock,
            return_value=_openai_http_response(content),
        ):
            result = await client.request(req, RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)
