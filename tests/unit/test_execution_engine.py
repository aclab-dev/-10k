"""Tests de ExecutionEngine (F10/CR)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from backend.core.config import Environment, load_config
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
from backend.exchange_adapters.schemas import OrderRequest, OrderResult, OrderStatus
from backend.execution.engine import ExecutionEngine, ExecutionTimeoutError
from backend.market_regime.schemas import PrimaryRegime
from backend.position_manager.manager import PositionManager
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult
from backend.storage.models import Order, Position, Trade

_NOW = datetime.now(UTC)


def _make_decision(
    *,
    decision_type: DecisionType = DecisionType.LONG,
    symbol: str = "BTCUSDT",
    entry_type: EntryType = EntryType.MARKET,
    entry_price: float = 50_100.0,
    stop_loss: float = 49_500.0,
    take_profit: float = 51_500.0,
    leverage: int = 3,
    margin_usdt: float = 5.0,
    use_trailing_stop: bool = False,
) -> ModelDecision:
    execute = decision_type != DecisionType.NO_OPERAR
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=decision_type,
        symbol=symbol,
        entry_type=entry_type,
        entry_price=entry_price,
        stop_loss=stop_loss if execute else 0.0,
        take_profit=take_profit if execute else 0.0,
        invalidation_price=49_000.0,
        leverage=leverage,
        margin_usdt=margin_usdt,
        estimated_notional_usdt=margin_usdt * leverage,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.3,
        estimated_max_loss_usdt=margin_usdt,
        liquidation_distance_percent_estimated=15.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_breakout_v1",
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
        news_context=NewsContextSection(
            used=False,
            impact=NewsImpact.NEUTRAL,
            summary="No news data used.",
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=use_trailing_stop,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="test fixture",
        execute=execute,
    )


def _make_risk_result(
    decision: ModelDecision,
    *,
    risk_decision: RiskDecision = RiskDecision.APPROVE,
    adjusted_margin_usdt: Decimal | None = None,
    adjusted_leverage: int | None = None,
) -> RiskValidationResult:
    adjusted = None
    if risk_decision in (RiskDecision.APPROVE, RiskDecision.ADJUST_DOWN):
        adjusted = AdjustedParameters(
            margin_usdt=adjusted_margin_usdt
            if adjusted_margin_usdt is not None
            else Decimal(str(decision.margin_usdt)),
            leverage=adjusted_leverage if adjusted_leverage is not None else decision.leverage,
        )
    return RiskValidationResult(
        aggregation_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=_NOW,
        decision=risk_decision,
        original_margin_usdt=Decimal(str(decision.margin_usdt)),
        original_leverage=decision.leverage,
        adjusted_parameters=adjusted,
        reasons={"test": "fixture"},
    )


def _engine(
    adapter,
    *,
    atr: Decimal | None = Decimal("500"),
    timeout_seconds: float = 5.0,
) -> tuple[ExecutionEngine, Mock, Mock]:
    session = Mock()
    volatility_repo = Mock()
    volatility_repo.get_latest_by_symbol.return_value = Mock(atr=atr) if atr is not None else None
    engine = ExecutionEngine(
        adapter=adapter,
        position_manager=PositionManager(adapter),
        session=session,
        bot_run_id="run-1",
        environment=Environment.PAPER,
        position_management_defaults=load_config().position_management,
        place_order_timeout_seconds=timeout_seconds,
    )
    engine._order_repo = Mock()  # type: ignore[attr-defined]
    engine._volatility_repo = volatility_repo  # type: ignore[attr-defined]
    return engine, session, engine._order_repo  # type: ignore[attr-defined]


def test_execute_approved_plan_fills_and_registers_position() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.status == OrderStatus.FILLED
    assert result.position_registered is True
    order_repo.save.assert_called_once()
    assert isinstance(order_repo.save.call_args[0][0], Order)
    trade_calls = [c for c in session.add.call_args_list if isinstance(c[0][0], Trade)]
    position_calls = [c for c in session.add.call_args_list if isinstance(c[0][0], Position)]
    assert len(trade_calls) == 1
    assert len(position_calls) == 1
    session.commit.assert_called_once()

    config = engine._position_manager.get_config(decision.symbol)  # type: ignore[attr-defined]
    assert config is not None
    assert config.stop_loss == Decimal(str(decision.stop_loss))
    assert config.take_profit == Decimal(str(decision.take_profit))


def test_execute_approved_plan_uses_adjusted_parameters_not_original() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, _order_repo = _engine(adapter)
    decision = _make_decision(margin_usdt=10.0, leverage=5)
    risk_result = _make_risk_result(
        decision,
        risk_decision=RiskDecision.ADJUST_DOWN,
        adjusted_margin_usdt=Decimal("3"),
        adjusted_leverage=2,
    )

    engine.execute_approved_plan(decision, risk_result)

    assert adapter.get_position(decision.symbol) is not None
    position = adapter.get_position(decision.symbol)
    assert position is not None
    assert position.leverage == 2  # el ajustado, no el original (5)
    expected_quantity = (Decimal("3") * 2) / Decimal(str(decision.entry_price))
    assert abs(position.quantity - expected_quantity) < Decimal("0.00000001")


@pytest.mark.parametrize("risk_decision", [RiskDecision.BLOCK, RiskDecision.NO_OPERAR])
def test_execute_approved_plan_rejects_non_executable_risk_decision(risk_decision) -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision, risk_decision=risk_decision)

    with pytest.raises(ValueError, match="no es ejecutable"):
        engine.execute_approved_plan(decision, risk_result)

    order_repo.save.assert_not_called()
    assert adapter.get_position(decision.symbol) is None


def test_execute_approved_plan_rejects_no_entry_type() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision(entry_type=EntryType.NO_ENTRY)
    risk_result = _make_risk_result(decision)

    with pytest.raises(ValueError, match="NO_ENTRY"):
        engine.execute_approved_plan(decision, risk_result)

    order_repo.save.assert_not_called()


def test_execute_approved_plan_fails_closed_without_atr() -> None:
    """Sin ATR reciente no se coloca ninguna orden (fail closed antes de tocar el adapter)."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter, atr=None)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    with pytest.raises(ValueError, match="VolatilityAssessment"):
        engine.execute_approved_plan(decision, risk_result)

    order_repo.save.assert_not_called()
    assert adapter.get_position(decision.symbol) is None


def test_execute_approved_plan_limit_order_pending_does_not_register_position() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, session, order_repo = _engine(adapter)
    decision = _make_decision(entry_type=EntryType.LIMIT)
    risk_result = _make_risk_result(decision)

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.status == OrderStatus.PENDING
    assert result.position_registered is False
    assert result.trade_id is None
    order_repo.save.assert_called_once()
    trade_calls = [c for c in session.add.call_args_list if isinstance(c[0][0], Trade)]
    assert trade_calls == []
    assert engine._position_manager.get_config(decision.symbol) is None  # type: ignore[attr-defined]


def test_execute_approved_plan_raises_on_timeout() -> None:
    class _SlowAdapter(PaperAdapter):
        def place_order(self, request: OrderRequest) -> OrderResult:
            time.sleep(0.2)
            return super().place_order(request)

    adapter = _SlowAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, _order_repo = _engine(adapter, timeout_seconds=0.05)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    with pytest.raises(ExecutionTimeoutError):
        engine.execute_approved_plan(decision, risk_result)
