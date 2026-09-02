"""Caos — timeouts (F16 [118]).

Inyecta cuelgues sin respuesta en las llamadas bloqueantes del bot y valida que
el sistema falla de forma segura: nunca finge éxito, nunca deja estado fantasma
persistido y el mismo componente sigue operativo para el ciclo siguiente.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from backend.core.config import Environment, get_config, load_config
from backend.core.retry import RetryConfig, retry_async
from backend.decision_engine.gpt_client import (
    GPTClient,
    GPTRequest,
    GPTTimeoutError,
    RequestPurpose,
)
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    EntryType,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    ModelDecision,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.execution.engine import ExecutionEngine, ExecutionTimeoutError
from backend.market_regime.schemas import PrimaryRegime
from backend.position_manager.manager import PositionManager
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult
from tests.chaos.faults import ChaosAdapter, InjectedTimeout

pytestmark = pytest.mark.chaos


# ---------------------------------------------------------------------------
# Fixtures de decisión (mínimas, alineadas con tests/unit/test_execution_engine.py)
# ---------------------------------------------------------------------------


def _decision(*, symbol: str = "BTCUSDT") -> ModelDecision:
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=datetime.now(UTC),
        decision=DecisionType.LONG,
        symbol=symbol,
        entry_type=EntryType.MARKET,
        entry_price=50_000.0,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        invalidation_price=48_500.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.0,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=20.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="chaos_timeout",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=QuantSignalsSection(
            momentum=MomentumInterpretation.BULLISH,
            mean_reversion=MeanReversionInterpretation.NEUTRAL,
            breakout_detection=BreakoutInterpretation.CONFIRMED,
            funding_analysis=FundingInterpretation.NEUTRAL,
            open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
            order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
            liquidity_sweep=LiquiditySweepInterpretation.NONE,
        ),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=0.85,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary="no news"),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="chaos timeout fixture",
        execute=True,
    )


def _risk_result(decision: ModelDecision) -> RiskValidationResult:
    return RiskValidationResult(
        aggregation_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=decision.timestamp_utc,
        decision=RiskDecision.APPROVE,
        original_margin_usdt=Decimal(str(decision.margin_usdt)),
        original_leverage=decision.leverage,
        adjusted_parameters=AdjustedParameters(
            margin_usdt=Decimal(str(decision.margin_usdt)), leverage=decision.leverage
        ),
        reasons={"test": "chaos fixture"},
    )


def _execution_engine(
    adapter: PaperAdapter | ChaosAdapter, *, timeout_seconds: float
) -> tuple[ExecutionEngine, Mock, Mock]:
    session = Mock()
    order_repo = Mock()
    order_repo.get_by_client_order_id.return_value = None
    volatility_repo = Mock()
    volatility_repo.get_latest_by_symbol.return_value = Mock(atr=Decimal("500"))
    engine = ExecutionEngine(
        adapter=adapter,
        position_manager=PositionManager(adapter),
        session=session,
        bot_run_id="run-1",
        environment=Environment.PAPER,
        position_management_defaults=load_config().position_management,
        place_order_timeout_seconds=timeout_seconds,
    )
    engine._order_repo = order_repo  # type: ignore[attr-defined]
    engine._volatility_repo = volatility_repo  # type: ignore[attr-defined]
    return engine, session, order_repo


# ---------------------------------------------------------------------------
# ExecutionEngine — place_order colgado
# ---------------------------------------------------------------------------


def test_place_order_timeout_leaves_no_phantom_state() -> None:
    """El adapter se cuelga en place_order → ExecutionTimeoutError y NADA se
    persiste ni queda abierto: no hay Order/Trade/Position ni posición en el
    adapter."""
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    adapter.hang("place_order", seconds=1.0)
    engine, session, order_repo = _execution_engine(adapter, timeout_seconds=0.1)
    decision = _decision()

    with pytest.raises(ExecutionTimeoutError):
        engine.execute_approved_plan(decision, _risk_result(decision))

    order_repo.save.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert inner.get_position(decision.symbol) is None


def test_execution_recovers_after_transient_place_order_timeout() -> None:
    """Cuelgue solo en la primera llamada: la segunda decisión se ejecuta normal
    (el pool no queda envenenado por el thread huérfano)."""
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    adapter.hang("place_order", seconds=0.5, times=1)
    engine, _session, _order_repo = _execution_engine(adapter, timeout_seconds=0.1)

    first = _decision(symbol="BTCUSDT")
    with pytest.raises(ExecutionTimeoutError):
        engine.execute_approved_plan(first, _risk_result(first))

    second = _decision(symbol="ETHUSDT")
    result = engine.execute_approved_plan(second, _risk_result(second))
    assert result.position_registered is True
    assert inner.get_position("ETHUSDT") is not None


# ---------------------------------------------------------------------------
# retry_async — nunca fabrica un éxito
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_async_reraises_on_persistent_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise InjectedTimeout("sin respuesta")

    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    config = RetryConfig(max_attempts=4, base_delay_seconds=0.0, jitter_ratio=0.0)

    with pytest.raises(InjectedTimeout):
        await retry_async(op, config=config, is_retryable=lambda _exc: True)

    assert calls == 4  # agotó todos los intentos, no devolvió un valor de relleno


# ---------------------------------------------------------------------------
# GPTClient — timeout en NEW_ENTRY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpt_timeout_blocks_new_entry_when_policy_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout de GPT en NEW_ENTRY con gpt_failure_blocks_new_entries=True →
    request() propaga el error (no devuelve una decisión inventada)."""
    policy = get_config().failure_policy.model_copy(update={"gpt_failure_blocks_new_entries": True})
    client = GPTClient(api_key="test-key", failure_policy=policy)
    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(client, "_call_once", AsyncMock(side_effect=GPTTimeoutError("timeout")))

    req = GPTRequest(system_prompt="s", user_prompt="u", prompt_version="v1")
    with pytest.raises(GPTTimeoutError):
        await client.request(req, RequestPurpose.NEW_ENTRY)

    await client.aclose()


@pytest.mark.asyncio
async def test_gpt_timeout_returns_none_when_policy_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con gpt_failure_blocks_new_entries=False el timeout se traga y devuelve
    None — el ciclo sigue sin abrir posición, nunca con una decisión falsa."""
    policy = get_config().failure_policy.model_copy(
        update={"gpt_failure_blocks_new_entries": False}
    )
    client = GPTClient(api_key="test-key", failure_policy=policy)
    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(client, "_call_once", AsyncMock(side_effect=GPTTimeoutError("timeout")))

    req = GPTRequest(system_prompt="s", user_prompt="u", prompt_version="v1")
    result = await client.request(req, RequestPurpose.NEW_ENTRY)
    assert result is None

    await client.aclose()
