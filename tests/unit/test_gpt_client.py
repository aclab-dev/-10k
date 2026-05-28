"""Tests unitarios para GPTClient — retry, timeout, failure policy (Decisión 08)."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.core.config import FailurePolicyConfig
from backend.decision_engine.gpt_client import (
    GPTAuthError,
    GPTClient,
    GPTClientConfig,
    GPTClientError,
    GPTRateLimitError,
    GPTRequest,
    GPTResponseValidationError,
    RequestPurpose,
)
from backend.decision_engine.schemas import (
    DecisionType,
    ModelDecision,
)

# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------

_TEST_API_KEY = "sk-test-key-not-real"

_POLICY_DEFAULT = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=True,
    token_budget_failure_blocks_new_entries=True,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)

# Política con todos los flags en False — GPT falla nunca bloquea
_POLICY_GPT_NEVER_BLOCKS = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=False,
    token_budget_failure_blocks_new_entries=False,
    deterministic_position_management_without_gpt=False,
    exits_do_not_require_gpt_response=False,
    every_position_requires_confirmed_sl_tp=True,
)


def _make_client(
    *,
    max_retries: int = 0,
    base_delay: float = 0.0,
    failure_policy: FailurePolicyConfig | None = None,
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
    )


def _make_request() -> GPTRequest:
    return GPTRequest(
        system_prompt="You are a test evaluator.",
        user_prompt="Evaluate this context.",
        prompt_version="1.0",
    )


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


def _openai_response(content: str, status: int = 200, finish_reason: str = "stop") -> MagicMock:
    """Crea un mock de httpx.Response para una respuesta de OpenAI Chat."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status
    mock_resp.text = content
    mock_resp.headers = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {"content": content, "role": "assistant"},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    return mock_resp


def _error_response(status: int, body: str = "error") -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status
    mock_resp.text = body
    mock_resp.headers = MagicMock()
    mock_resp.headers.get.return_value = None
    return mock_resp


def _rate_limit_response(retry_after: str | None = None) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 429
    mock_resp.text = "Rate limit exceeded"
    mock_resp.headers = MagicMock()
    mock_resp.headers.get.side_effect = lambda k, d=None: (
        retry_after if k == "Retry-After" and retry_after else d
    )
    return mock_resp


def _empty_choices_response() -> MagicMock:
    """Respuesta 200 pero con choices vacío — estructura inesperada."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.text = ""
    mock_resp.headers = MagicMock()
    mock_resp.json.return_value = {"choices": [], "usage": {}}
    return mock_resp


# ---------------------------------------------------------------------------
# Tests: inicialización
# ---------------------------------------------------------------------------


def test_missing_api_key_raises() -> None:
    """Sin OPENAI_API_KEY en env ni argumento → GPTAuthError en __init__."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(GPTAuthError, match="OPENAI_API_KEY"):
            GPTClient(
                config=GPTClientConfig(),
                failure_policy=_POLICY_DEFAULT,
            )


def test_explicit_api_key_accepted() -> None:
    """api_key explícita no requiere env var."""
    client = _make_client()
    assert client is not None


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_success_returns_model_decision() -> None:
    """Happy path: respuesta válida → retorna ModelDecision."""
    client = _make_client()
    content = json.dumps(_valid_decision_dict())
    mock_resp = _openai_response(content)

    with patch.object(client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert isinstance(result, ModelDecision)
    assert result.decision == DecisionType.NO_OPERAR


# ---------------------------------------------------------------------------
# Tests: retry en errores transitorios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_rate_limit_succeeds_on_second_attempt() -> None:
    """429 en primer intento → reintenta → éxito en segundo."""
    client = _make_client(max_retries=2, base_delay=0.0)
    content = json.dumps(_valid_decision_dict())

    side_effects = [_rate_limit_response(), _openai_response(content)]
    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        side_effect=side_effects,
    ):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert isinstance(result, ModelDecision)
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_after_header_sets_delay() -> None:
    """Retry-After header en 429 → delay usa el valor del header."""
    client = _make_client(max_retries=2, base_delay=1.0)
    content = json.dumps(_valid_decision_dict())

    side_effects = [_rate_limit_response(retry_after="7"), _openai_response(content)]
    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        side_effect=side_effects,
    ):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert isinstance(result, ModelDecision)
    mock_sleep.assert_called_once_with(7.0)


@pytest.mark.asyncio
async def test_retry_on_timeout_succeeds_on_second_attempt() -> None:
    """Timeout en primer intento → reintenta → éxito en segundo."""
    client = _make_client(max_retries=2, base_delay=0.0)
    content = json.dumps(_valid_decision_dict())
    call_count = 0

    async def _post_with_recovery(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.TimeoutException("timed out")
        return _openai_response(content)

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
async def test_retry_on_server_error_succeeds_on_second_attempt() -> None:
    """500 en primer intento → reintenta → éxito en segundo."""
    client = _make_client(max_retries=2, base_delay=0.0)
    content = json.dumps(_valid_decision_dict())

    side_effects = [
        _error_response(500, "Internal Server Error"),
        _openai_response(content),
    ]
    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        side_effect=side_effects,
    ):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert isinstance(result, ModelDecision)


# ---------------------------------------------------------------------------
# Tests: retries agotados
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_retries_rate_limit_raises() -> None:
    """Retries agotados con 429 en NEW_ENTRY → raise GPTRateLimitError."""
    client = _make_client(max_retries=2, base_delay=0.0)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_rate_limit_response(),
    ):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(GPTRateLimitError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


@pytest.mark.asyncio
async def test_exhausted_retries_server_error_raises() -> None:
    """Retries agotados con 500 en NEW_ENTRY → raise GPTClientError."""
    client = _make_client(max_retries=1, base_delay=0.0)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_error_response(503),
    ):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(GPTClientError):
                await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


# ---------------------------------------------------------------------------
# Tests: errores no reintentables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_on_auth_error() -> None:
    """401 → GPTAuthError sin reintento (1 sola llamada HTTP)."""
    client = _make_client(max_retries=3, base_delay=0.0)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_error_response(401),
    ) as mock_post:
        with pytest.raises(GPTAuthError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_validation_error() -> None:
    """JSON inválido → GPTResponseValidationError sin reintento."""
    client = _make_client(max_retries=3, base_delay=0.0)
    bad_content = "not valid json {{{"
    mock_resp = _openai_response(bad_content)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ) as mock_post:
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_schema_mismatch() -> None:
    """JSON válido pero fuera del schema → GPTResponseValidationError sin reintento."""
    client = _make_client(max_retries=3, base_delay=0.0)
    bad_schema = json.dumps({"foo": "bar"})
    mock_resp = _openai_response(bad_schema)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ) as mock_post:
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Tests: failure policy — Decisión 08
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_policy_new_entry_blocks_on_error() -> None:
    """NEW_ENTRY + gpt_failure_blocks_new_entries=True → raise."""
    client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_rate_limit_response(),
    ):
        with pytest.raises(GPTRateLimitError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


@pytest.mark.asyncio
async def test_failure_policy_new_entry_swallowed_when_blocks_disabled() -> None:
    """NEW_ENTRY + gpt_failure_blocks_new_entries=False → retorna None (swallow)."""
    policy = FailurePolicyConfig(
        gpt_failure_blocks_new_entries=False,
        token_budget_failure_blocks_new_entries=False,
        deterministic_position_management_without_gpt=True,
        exits_do_not_require_gpt_response=True,
        every_position_requires_confirmed_sl_tp=True,
    )
    client = _make_client(max_retries=0, failure_policy=policy)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_rate_limit_response(),
    ):
        result = await client.request(_make_request(), RequestPurpose.NEW_ENTRY)

    assert result is None


@pytest.mark.asyncio
async def test_failure_policy_position_management_returns_none() -> None:
    """POSITION_MANAGEMENT + deterministic=True → retorna None en vez de raise."""
    client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_rate_limit_response(),
    ):
        result = await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)

    assert result is None


@pytest.mark.asyncio
async def test_failure_policy_position_management_raises_when_policy_disabled() -> None:
    """POSITION_MANAGEMENT con deterministic=False → raise igual."""
    client = _make_client(max_retries=0, failure_policy=_POLICY_GPT_NEVER_BLOCKS)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_rate_limit_response(),
    ):
        with pytest.raises(GPTRateLimitError):
            await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)


@pytest.mark.asyncio
async def test_failure_policy_validation_error_new_entry_blocks() -> None:
    """Schema inválido en NEW_ENTRY → raise GPTResponseValidationError."""
    client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT)
    bad = json.dumps({"invalid": "schema"})

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_openai_response(bad),
    ):
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


@pytest.mark.asyncio
async def test_validation_error_always_raises_despite_position_management_fallback() -> None:
    """Schema inválido en POSITION_MANAGEMENT → raise aunque fallback esté activo.

    La failure policy no aplica a respuestas inválidas — una respuesta corrupta
    no es 'GPT no disponible'. Siempre bloquea.
    """
    client = _make_client(max_retries=0, failure_policy=_POLICY_DEFAULT)
    bad = json.dumps({"invalid": "schema"})

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_openai_response(bad),
    ):
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.POSITION_MANAGEMENT)


@pytest.mark.asyncio
async def test_validation_error_always_raises_when_new_entry_blocks_disabled() -> None:
    """Schema inválido en NEW_ENTRY con blocks=False → raise igual.

    gpt_failure_blocks_new_entries=False silencia errores de conectividad pero
    nunca respuestas inválidas — una respuesta que no pasa el schema guard
    no puede derivar en un trade.
    """
    policy = FailurePolicyConfig(
        gpt_failure_blocks_new_entries=False,
        token_budget_failure_blocks_new_entries=False,
        deterministic_position_management_without_gpt=True,
        exits_do_not_require_gpt_response=True,
        every_position_requires_confirmed_sl_tp=True,
    )
    client = _make_client(max_retries=0, failure_policy=policy)
    bad = json.dumps({"totally": "wrong"})

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_openai_response(bad),
    ):
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


@pytest.mark.asyncio
async def test_no_json_response_always_raises_when_blocks_disabled() -> None:
    """Respuesta no-JSON en NEW_ENTRY con blocks=False → raise GPTResponseValidationError."""
    policy = FailurePolicyConfig(
        gpt_failure_blocks_new_entries=False,
        token_budget_failure_blocks_new_entries=False,
        deterministic_position_management_without_gpt=True,
        exits_do_not_require_gpt_response=True,
        every_position_requires_confirmed_sl_tp=True,
    )
    client = _make_client(max_retries=0, failure_policy=policy)

    with patch.object(
        client._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=_openai_response("not json at all <<<"),
    ):
        with pytest.raises(GPTResponseValidationError):
            await client.request(_make_request(), RequestPurpose.NEW_ENTRY)


# ---------------------------------------------------------------------------
# Tests: extracción de contenido HTTP
# ---------------------------------------------------------------------------


def test_extract_content_unexpected_status_raises() -> None:
    """Status inesperado (ej. 422) → GPTClientError."""
    client = _make_client()
    mock_resp = _error_response(422, "Unprocessable")
    with pytest.raises(GPTClientError, match="422"):
        client._extract_content(mock_resp, "gpt-4o")


def test_extract_content_403_raises_auth_error() -> None:
    """403 → GPTAuthError."""
    client = _make_client()
    mock_resp = _error_response(403)
    with pytest.raises(GPTAuthError):
        client._extract_content(mock_resp, "gpt-4o")


def test_extract_content_rate_limit_carries_retry_after() -> None:
    """429 con Retry-After header → GPTRateLimitError con retry_after_seconds."""
    client = _make_client()
    mock_resp = _rate_limit_response(retry_after="15")
    with pytest.raises(GPTRateLimitError) as exc_info:
        client._extract_content(mock_resp, "gpt-4o")
    assert exc_info.value.retry_after_seconds == 15.0


def test_extract_content_rate_limit_without_retry_after() -> None:
    """429 sin Retry-After → GPTRateLimitError con retry_after_seconds=None."""
    client = _make_client()
    mock_resp = _rate_limit_response()
    with pytest.raises(GPTRateLimitError) as exc_info:
        client._extract_content(mock_resp, "gpt-4o")
    assert exc_info.value.retry_after_seconds is None


def test_extract_content_empty_choices_raises() -> None:
    """choices: [] → GPTClientError con mensaje de estructura inesperada."""
    client = _make_client()
    mock_resp = _empty_choices_response()
    with pytest.raises(GPTClientError, match="estructura inesperada"):
        client._extract_content(mock_resp, "gpt-4o")


def test_extract_content_finish_reason_length_raises() -> None:
    """finish_reason='length' → GPTClientError (respuesta truncada)."""
    client = _make_client()
    mock_resp = _openai_response("partial content...", finish_reason="length")
    with pytest.raises(GPTClientError, match="truncada"):
        client._extract_content(mock_resp, "gpt-4o")
