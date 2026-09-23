"""Tests de ExecutionEngine (F10/CR)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from backend.core.config import Environment, load_config
from backend.core.slippage import SlippageEstimate, estimate_slippage
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
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
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
    move_to_break_even: bool = False,
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
            move_to_break_even=move_to_break_even,
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
    order_repo = Mock()
    order_repo.get_by_client_order_id.return_value = None  # sin retry por default
    engine._order_repo = order_repo  # type: ignore[attr-defined]
    engine._volatility_repo = volatility_repo  # type: ignore[attr-defined]
    return engine, session, order_repo


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


def test_execute_approved_plan_maps_be_sl_offset_from_fill_price_not_decision_entry() -> None:
    """be_sl_offset se calcula sobre el fill_price real (post-slippage), no sobre el
    entry_price de la decisión: con PAPER, la orden MARKET sufre 2 BPS de slippage
    adverso (SlippageModel), así que difieren y hay que probar que se usa el correcto."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, _order_repo = _engine(adapter)
    decision = _make_decision(move_to_break_even=True)
    risk_result = _make_risk_result(decision)

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.fill_price is not None
    assert result.order_result.fill_price != Decimal(str(decision.entry_price))

    config = engine._position_manager.get_config(decision.symbol)  # type: ignore[attr-defined]
    assert config is not None
    be_sl_offset_percent = Decimal(str(load_config().position_management.be_sl_offset_percent))
    assert config.be_sl_offset == result.order_result.fill_price * be_sl_offset_percent


def test_execute_approved_plan_raises_on_filled_without_fill_price() -> None:
    """Guard defensivo de engine.py: si el adapter devolviera FILLED sin fill_price
    (violación de su propia invariante), no debe registrarse un PositionManager con
    entry_price desconocido — mejor fallar ruidosamente.

    La orden ya fue colocada en el exchange en este punto, así que el order_row
    tiene que quedar persistido (commit) ANTES del raise: sin fila en `orders`,
    un retry del mismo decision_id no la encontraría vía get_by_client_order_id
    y colocaría una segunda orden real (rompe idempotencia) además de dejar una
    ejecución sin registro auditable."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    bad_result = OrderResult(
        client_order_id="test-client-order-id",
        symbol=decision.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        quantity_requested=Decimal("1"),
        quantity_filled=Decimal("1"),
        fill_price=None,
        is_simulated=True,
        timestamp_utc=_NOW,
    )
    engine._adapter.place_order = Mock(return_value=bad_result)  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="FILLED sin fill_price"):
        engine.execute_approved_plan(decision, risk_result)

    order_repo.save.assert_called_once()
    persisted_order = order_repo.save.call_args[0][0]
    assert persisted_order.client_order_id == "test-client-order-id"
    session.commit.assert_called_once()
    assert engine._position_manager.get_config(decision.symbol) is None  # type: ignore[attr-defined]


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


def test_execute_approved_plan_is_idempotent_on_retry() -> None:
    """Un segundo execute_approved_plan() con el mismo decision_id no debe duplicar la orden."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    existing_order = Order(
        id="order-db-1",
        bot_run_id="run-1",
        trade_id="trade-1",
        client_order_id=decision.decision_id,
        symbol=decision.symbol,
        environment="PAPER",
        order_type="MARKET",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal(str(decision.entry_price)),
        status="FILLED",
        exchange_order_id="exch-1",
        filled_at=_NOW,
        fill_price=Decimal(str(decision.entry_price)),
        fee=Decimal("0.01"),
        is_simulated=True,
    )
    order_repo.get_by_client_order_id.return_value = existing_order

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.trade_id == "trade-1"
    assert result.order_db_id == "order-db-1"
    assert result.position_registered is True
    assert result.order_result.status == OrderStatus.FILLED
    order_repo.save.assert_not_called()
    # No se volvio a llamar place_order: el adapter no tiene ninguna posicion.
    assert adapter.get_position(decision.symbol) is None


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


def test_timeout_does_not_block_subsequent_calls() -> None:
    """El thread colgado tras un timeout no debe encolar llamadas siguientes detras suyo.

    ThreadPoolExecutor no puede cancelar un thread ya iniciado — sin descartar
    y recrear el pool tras el timeout, esta llamada quedaria esperando el
    sleep(0.3) del primer thread colgado antes de poder correr.
    """

    class _SlowOnceAdapter(PaperAdapter):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.calls = 0

        def place_order(self, request: OrderRequest) -> OrderResult:
            self.calls += 1
            if self.calls == 1:
                time.sleep(0.3)
            return super().place_order(request)

    adapter = _SlowOnceAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, _order_repo = _engine(adapter, timeout_seconds=0.05)
    decision_1 = _make_decision(symbol="BTCUSDT")
    decision_2 = _make_decision(symbol="ETHUSDT")

    with pytest.raises(ExecutionTimeoutError):
        engine.execute_approved_plan(decision_1, _make_risk_result(decision_1))

    start = time.monotonic()
    result = engine.execute_approved_plan(decision_2, _make_risk_result(decision_2))
    elapsed = time.monotonic() - start

    assert result.order_result.status == OrderStatus.FILLED
    assert elapsed < 0.15  # muy por debajo del sleep(0.3) del thread colgado


# ---------------------------------------------------------------------------
# Persistencia del slippage (F17 [162], regla no negociable 13)
# ---------------------------------------------------------------------------


def _slippage_estimate(estimated_usdt: str = "0.02") -> SlippageEstimate:
    return SlippageEstimate(
        estimated_slippage_usdt=Decimal(estimated_usdt),
        half_spread_usdt=Decimal("0.01"),
        impact_usdt=Decimal("0.01"),
        expected_fill_price=Decimal("50110"),
        bid=Decimal("50090"),
        ask=Decimal("50110"),
    )


def test_execute_approved_plan_persists_real_slippage_on_fill() -> None:
    """El slippage real del adapter deja de perderse: va a orders.slippage_usdt."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    result = engine.execute_approved_plan(decision, risk_result)

    saved_order: Order = order_repo.save.call_args[0][0]
    # PaperAdapter aplica 2 BPS adversos en MARKET, así que el real es > 0 y es
    # exactamente el que reportó el adapter — no un 0 hardcodeado ni un recálculo.
    assert result.order_result.slippage_usdt > Decimal("0")
    assert saved_order.slippage_usdt == result.order_result.slippage_usdt


def test_execute_approved_plan_persists_estimated_slippage() -> None:
    """El estimado pre-trade queda en la misma fila, para comparar contra el real."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    engine.execute_approved_plan(
        decision, risk_result, slippage_estimate=_slippage_estimate("0.02")
    )

    saved_order: Order = order_repo.save.call_args[0][0]
    assert saved_order.estimated_slippage_usdt == Decimal("0.02")
    assert saved_order.slippage_usdt is not None


def test_execute_approved_plan_without_estimate_persists_null_not_zero() -> None:
    """Sin estimación se guarda NULL: 'no se estimó' no es 'se estimó y dio 0'."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()

    engine.execute_approved_plan(decision, _make_risk_result(decision))

    saved_order: Order = order_repo.save.call_args[0][0]
    assert saved_order.estimated_slippage_usdt is None


def test_unfilled_order_persists_null_real_slippage() -> None:
    """Una orden que no llenó no tiene slippage real: NULL, no el 0 del default."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision(entry_type=EntryType.LIMIT)
    risk_result = _make_risk_result(decision)

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.status != OrderStatus.FILLED
    saved_order: Order = order_repo.save.call_args[0][0]
    assert saved_order.slippage_usdt is None


def test_idempotent_replay_returns_persisted_slippage_not_zero() -> None:
    """El replay idempotente lee orders.slippage_usdt en vez de devolver 0 fijo.

    Era el síntoma del gap de la regla 13: el valor no se persistía, así que un
    retry del mismo plan devolvía siempre slippage 0 y la auditoría mentía.
    """
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    order_repo.get_by_client_order_id.return_value = Order(
        id="order-db-1",
        bot_run_id="run-1",
        trade_id="trade-1",
        client_order_id=decision.decision_id,
        symbol=decision.symbol,
        environment="PAPER",
        order_type="MARKET",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal(str(decision.entry_price)),
        status="FILLED",
        exchange_order_id="exch-1",
        filled_at=_NOW,
        fill_price=Decimal(str(decision.entry_price)),
        fee=Decimal("0.01"),
        slippage_usdt=Decimal("0.03"),
        is_simulated=True,
    )

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.slippage_usdt == Decimal("0.03")


def test_idempotent_replay_reports_none_for_pre_migration_orders() -> None:
    """Órdenes anteriores a la migración e5b3a71c9d40 no tienen el dato: None, no 0.

    `None` significa "no se midió". Devolver 0 las haría indistinguibles de un
    fill sin slippage, que es justo el sesgo que la columna viene a evitar.
    """
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    risk_result = _make_risk_result(decision)

    order_repo.get_by_client_order_id.return_value = Order(
        id="order-db-1",
        bot_run_id="run-1",
        trade_id="trade-1",
        client_order_id=decision.decision_id,
        symbol=decision.symbol,
        environment="PAPER",
        order_type="MARKET",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal(str(decision.entry_price)),
        status="FILLED",
        exchange_order_id="exch-1",
        filled_at=_NOW,
        fill_price=Decimal(str(decision.entry_price)),
        fee=Decimal("0.01"),
        slippage_usdt=None,
        is_simulated=True,
    )

    result = engine.execute_approved_plan(decision, risk_result)

    assert result.order_result.slippage_usdt is None


class _UnmeasuredSlippageAdapter(PaperAdapter):
    """Adapter que llena pero no mide slippage, como BingX (`slippage_usdt=None`)."""

    def place_order(self, request: OrderRequest) -> OrderResult:
        filled = super().place_order(request)
        return filled.model_copy(update={"slippage_usdt": None, "is_simulated": False})


def test_adapter_that_does_not_measure_slippage_persists_null_not_zero() -> None:
    """Un adapter real que no informa slippage no puede quedar registrado como 0.

    BingX devuelve `slippage_usdt=None` (no lo reporta). Persistir 0 haría pasar
    "no se midió" por "se midió y no hubo", y en TESTNET/LIVE dejaría toda orden
    llenada con estimado > 0 y real 0 — el sesgo que esta card viene a corregir.
    """
    engine, _session, order_repo = _engine(
        _UnmeasuredSlippageAdapter(initial_balance_usdt=Decimal("1000"))
    )
    decision = _make_decision()

    engine.execute_approved_plan(
        decision, _make_risk_result(decision), slippage_estimate=_slippage_estimate("0.02")
    )

    saved_order: Order = order_repo.save.call_args[0][0]
    assert saved_order.slippage_usdt is None
    # El estimado sí se guarda: el gap es la medición real, no la estimación.
    assert saved_order.estimated_slippage_usdt == Decimal("0.02")


def test_estimated_and_real_slippage_match_in_paper() -> None:
    """Estimado y real coinciden en PAPER, porque parten del mismo libro.

    `SlippageEstimate.bid/ask` viaja hasta la `OrderRequest`, así que el fill
    simulado cruza exactamente el spread contra el que se estimó. Si cada lado
    usara su propia fuente, la diferencia mediría la discrepancia entre dos
    entradas en vez del error del modelo.

    Coincidir es lo esperado en PAPER y no valida la heurística: ambos lados
    salen del mismo modelo. El error real sólo se mide contra fills de
    exchange, en TESTNET/LIVE.
    """
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    engine, _session, order_repo = _engine(adapter)
    decision = _make_decision()
    entry = Decimal(str(decision.entry_price))
    estimate = estimate_slippage(
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        notional_usdt=Decimal(str(decision.margin_usdt)) * decision.leverage,
        bid=entry - Decimal("10"),
        ask=entry + Decimal("10"),
        reference_price=entry,
        market_impact_bps=Decimal("2"),
    )

    result = engine.execute_approved_plan(
        decision, _make_risk_result(decision), slippage_estimate=estimate
    )

    assert result.order_result.fill_price == estimate.expected_fill_price
    saved_order: Order = order_repo.save.call_args[0][0]
    # La diferencia residual es el ROUND_DOWN de la cantidad ejecutada, muy por
    # debajo de un céntimo: el estimado se calcula sobre el notional sin cuantizar.
    assert saved_order.slippage_usdt is not None
    assert saved_order.estimated_slippage_usdt is not None
    assert abs(saved_order.slippage_usdt - saved_order.estimated_slippage_usdt) < Decimal("0.0001")
