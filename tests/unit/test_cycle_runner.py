"""Tests del CycleRunner."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.core.config import Environment, load_config
from backend.core.slippage import estimate_for_decision
from backend.decision_engine.aggregator_schemas import (
    ContributingSources,
    DecisionAggregationResult,
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
from backend.execution.engine import ExecutionEngine
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.engine import MarketRegimeEngine
from backend.market_regime.schemas import PrimaryRegime
from backend.position_manager.tick_service import PositionTickService
from backend.reconciliation.gate import ReconciliationGate
from backend.risk_engine import engine as risk_engine
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult
from backend.storage.database import Base
from backend.storage.models import BotRun
from backend.storage.models import BotState as BotStateRow
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import (
    DEFAULT_INTERVAL_SECONDS,
    CycleRunner,
    parse_interval_from_env,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()


def _make_bot_run(session: Session) -> BotRun:
    bot_run = BotRun(environment="PAPER", app_version="0.1.0", config_snapshot={}, status="RUNNING")
    session.add(bot_run)
    session.commit()
    return bot_run


@pytest.fixture
def heartbeat_file(tmp_path: Path) -> Path:
    return tmp_path / "worker_alive"


def test_interval_zero_or_negative_raises(heartbeat_file: Path) -> None:
    sm = BotStateMachine()
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        CycleRunner(sm, interval_seconds=0, heartbeat_file=heartbeat_file)
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        CycleRunner(sm, interval_seconds=-1, heartbeat_file=heartbeat_file)


def test_run_exits_immediately_when_shutdown_set_before_start(
    heartbeat_file: Path,
) -> None:
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=60, heartbeat_file=heartbeat_file)
    runner.request_shutdown()
    runner.run()
    # No deberia haber tocado el archivo porque el loop nunca entro.
    assert not heartbeat_file.exists()


def test_tick_touches_heartbeat_when_running(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)
    runner._tick()  # type: ignore[attr-defined]
    assert heartbeat_file.exists()


def test_run_loops_and_exits_on_shutdown(heartbeat_file: Path) -> None:
    """Arranca el runner en otro thread y pide shutdown despues de unos ticks."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        # Esperar a que el primer tick toque el archivo (deberia ser inmediato).
        deadline = threading.Event()
        deadline.wait(timeout=2.0)
        assert heartbeat_file.exists()
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)
    assert not thread.is_alive(), "Thread should exit after shutdown"


def test_run_skips_tick_but_keeps_heartbeat_when_state_machine_not_running(
    heartbeat_file: Path,
) -> None:
    """En estado HALTED el runner no tickea servicios, pero sigue vivo: el
    heartbeat se refresca igual para que el healthcheck del container no lo
    marque unhealthy (F16 [157])."""
    sm = BotStateMachine(initial=BotState.HALTED)
    tick_service = Mock(spec=PositionTickService)
    runner = CycleRunner(
        sm, interval_seconds=1, heartbeat_file=heartbeat_file, position_tick_service=tick_service
    )

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        # Dar tiempo a que itere al menos una vez.
        threading.Event().wait(timeout=1.5)
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)

    assert heartbeat_file.exists()
    tick_service.tick_all.assert_not_called()


def test_run_keeps_heartbeat_after_inherited_kill_switch(heartbeat_file: Path) -> None:
    """Regresion F16 [157]: worker que arranca heredando KILL_SWITCH_TRIGGERED
    de un bot_run anterior (caso normal tras el kill switch manual de F15 +
    restart del worker). El loop entra en paused_by_state y nunca llama a
    _tick(), pero el proceso esta vivo y el healthcheck de docker-compose
    (`find /tmp/worker_alive -mmin -2`) no debe verlo unhealthy."""
    sm = BotStateMachine(initial=BotState.KILL_SWITCH_TRIGGERED)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    assert not sm.is_running()

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        threading.Event().wait(timeout=1.5)
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)

    assert not thread.is_alive(), "Thread should exit after shutdown"
    assert heartbeat_file.exists()


def test_tick_calls_position_tick_service_when_provided(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    tick_service = Mock(spec=PositionTickService)
    runner = CycleRunner(
        sm, interval_seconds=1, heartbeat_file=heartbeat_file, position_tick_service=tick_service
    )

    runner._tick()  # type: ignore[attr-defined]

    tick_service.tick_all.assert_called_once()


def test_tick_without_position_tick_service_still_heartbeats(heartbeat_file: Path) -> None:
    """Compat: position_tick_service es opcional y default None."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    runner._tick()  # type: ignore[attr-defined]

    assert heartbeat_file.exists()


def test_tick_calls_market_data_service_when_provided(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    market_data_service = Mock(spec=MarketDataCycleService)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        market_data_service=market_data_service,
    )

    runner._tick()  # type: ignore[attr-defined]

    market_data_service.tick_all.assert_called_once()


def test_tick_without_market_data_service_still_heartbeats(heartbeat_file: Path) -> None:
    """Compat: market_data_service es opcional y default None."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    runner._tick()  # type: ignore[attr-defined]

    assert heartbeat_file.exists()


def test_tick_calls_market_data_before_position_tick_service(heartbeat_file: Path) -> None:
    """Market data debe tickear antes que posiciones (datos frescos para el resto del ciclo)."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    call_order: list[str] = []
    market_data_service = Mock(spec=MarketDataCycleService)
    market_data_service.tick_all.side_effect = lambda: call_order.append("market_data")
    tick_service = Mock(spec=PositionTickService)
    tick_service.tick_all.side_effect = lambda: call_order.append("position")
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        market_data_service=market_data_service,
        position_tick_service=tick_service,
    )

    runner._tick()  # type: ignore[attr-defined]

    assert call_order == ["market_data", "position"]


def test_tick_calls_connection_health_monitor_when_provided(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    monitor = Mock(spec=ConnectionHealthMonitor)
    runner = CycleRunner(
        sm, interval_seconds=1, heartbeat_file=heartbeat_file, connection_health_monitor=monitor
    )

    runner._tick()  # type: ignore[attr-defined]

    monitor.check_and_enforce.assert_called_once()


def test_tick_without_connection_health_monitor_still_heartbeats(heartbeat_file: Path) -> None:
    """Compat: connection_health_monitor es opcional y default None."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    runner._tick()  # type: ignore[attr-defined]

    assert heartbeat_file.exists()


def test_tick_calls_market_data_before_connection_health_monitor(heartbeat_file: Path) -> None:
    """El monitor de salud de conexion evalua los snapshots del mismo ciclo."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    call_order: list[str] = []
    market_data_service = Mock(spec=MarketDataCycleService)
    market_data_service.tick_all.side_effect = lambda: call_order.append("market_data") or []
    monitor = Mock(spec=ConnectionHealthMonitor)
    monitor.check_and_enforce.side_effect = lambda snapshots: call_order.append("connection_health")
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        market_data_service=market_data_service,
        connection_health_monitor=monitor,
    )

    runner._tick()  # type: ignore[attr-defined]

    assert call_order == ["market_data", "connection_health"]


def test_tick_calls_reconciliation_gate_when_provided(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    gate = Mock(spec=ReconciliationGate)
    runner = CycleRunner(
        sm, interval_seconds=1, heartbeat_file=heartbeat_file, reconciliation_gate=gate
    )

    runner._tick()  # type: ignore[attr-defined]

    gate.run_and_enforce.assert_called_once()


def test_tick_without_reconciliation_gate_still_heartbeats(heartbeat_file: Path) -> None:
    """Compat: reconciliation_gate es opcional y default None."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    runner._tick()  # type: ignore[attr-defined]

    assert heartbeat_file.exists()


def test_tick_calls_position_tick_service_before_reconciliation_gate(heartbeat_file: Path) -> None:
    """El gate reconcilia contra el estado de posiciones ya actualizado del ciclo."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    call_order: list[str] = []
    tick_service = Mock(spec=PositionTickService)
    tick_service.tick_all.side_effect = lambda: call_order.append("position")
    gate = Mock(spec=ReconciliationGate)
    gate.run_and_enforce.side_effect = lambda: call_order.append("reconciliation")
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        position_tick_service=tick_service,
        reconciliation_gate=gate,
    )

    runner._tick()  # type: ignore[attr-defined]

    assert call_order == ["position", "reconciliation"]


def test_execution_engine_is_stored_and_exposed_but_not_auto_invoked(
    heartbeat_file: Path,
) -> None:
    """execution_engine queda disponible via property, pero _tick() no lo dispara aun (CR).

    No hay (todavia) fuente de decisiones en vivo (Aggregator/Risk/GPT sin
    wirear al ciclo real) — el wireo automatico queda para una fase posterior.
    """
    sm = BotStateMachine(initial=BotState.ACTIVE)
    execution_engine = Mock(spec=ExecutionEngine)
    runner = CycleRunner(
        sm, interval_seconds=1, heartbeat_file=heartbeat_file, execution_engine=execution_engine
    )

    assert runner.execution_engine is execution_engine

    runner._tick()  # type: ignore[attr-defined]

    execution_engine.execute_approved_plan.assert_not_called()


def test_request_shutdown_is_idempotent(heartbeat_file: Path) -> None:
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)
    runner.request_shutdown()
    runner.request_shutdown()  # No debe lanzar.
    assert runner.shutdown_requested is True


# -- parse_interval_from_env --


def test_parse_interval_default_when_none() -> None:
    assert parse_interval_from_env(None) == DEFAULT_INTERVAL_SECONDS


def test_parse_interval_default_when_empty() -> None:
    assert parse_interval_from_env("") == DEFAULT_INTERVAL_SECONDS


def test_parse_interval_valid_int() -> None:
    assert parse_interval_from_env("30") == 30


def test_parse_interval_non_int_raises() -> None:
    with pytest.raises(ValueError, match="must be an int"):
        parse_interval_from_env("abc")


def test_parse_interval_negative_or_zero_raises() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        parse_interval_from_env("0")
    with pytest.raises(ValueError, match="must be > 0"):
        parse_interval_from_env("-5")


# -- _sync_state_from_db --


def test_sync_state_from_db_is_noop_without_session_or_bot_run_id(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    runner._sync_state_from_db()  # type: ignore[attr-defined]

    assert sm.state == BotState.ACTIVE


def test_sync_state_from_db_adopts_persisted_kill_switch(
    heartbeat_file: Path, db_session: Session
) -> None:
    """El worker debe enterarse de un kill switch disparado desde la API (otro proceso)."""
    bot_run = _make_bot_run(db_session)
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        session=db_session,
        bot_run_id=bot_run.id,
    )

    db_session.add(
        BotStateRow(
            bot_run_id=bot_run.id,
            state="KILL_SWITCH_TRIGGERED",
            previous_state="ACTIVE",
            reason="kill switch manual",
        )
    )
    db_session.commit()

    runner._sync_state_from_db()  # type: ignore[attr-defined]

    assert sm.state == BotState.KILL_SWITCH_TRIGGERED
    assert not sm.is_running()


def test_sync_state_from_db_ignores_unknown_persisted_state(
    heartbeat_file: Path, db_session: Session
) -> None:
    bot_run = _make_bot_run(db_session)
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        session=db_session,
        bot_run_id=bot_run.id,
    )

    db_session.add(BotStateRow(bot_run_id=bot_run.id, state="IDLE", reason="test"))
    db_session.commit()

    runner._sync_state_from_db()  # type: ignore[attr-defined]

    assert sm.state == BotState.ACTIVE


def test_sync_state_from_db_survives_db_error(
    heartbeat_file: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un error transitorio de DB (conexion caida, timeout) no debe tumbar el
    loop entero: debe loguear, hacer rollback y seguir con el ultimo estado
    local conocido, para poder recuperarse en el proximo ciclo."""
    bot_run = _make_bot_run(db_session)
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        session=db_session,
        bot_run_id=bot_run.id,
    )

    def _raise(self: object, bot_run_id: str) -> None:
        raise OperationalError("select", {}, Exception("connection lost"))

    monkeypatch.setattr("backend.trading_core.cycle_runner.BotStateRepository.get_latest", _raise)
    rollback_calls: list[bool] = []
    monkeypatch.setattr(db_session, "rollback", lambda: rollback_calls.append(True))

    runner._sync_state_from_db()  # type: ignore[attr-defined]  # no debe lanzar

    assert sm.state == BotState.ACTIVE
    assert rollback_calls == [True]


def test_run_calls_sync_state_from_db_each_iteration(heartbeat_file: Path) -> None:
    """Regresion: run() debe releer el estado persistido en cada vuelta del loop,

    no solo al construir el runner. `>= 1` no distingue "cada iteracion" de
    "una sola vez antes del while" (esa era la regresion original); con
    interval_seconds=1 y 2.5s de espera hay margen para al menos 2 vueltas
    completas, asi que `>= 2` si prueba que se repite."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)
    sync_mock = Mock()
    runner._sync_state_from_db = sync_mock  # type: ignore[method-assign]

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        threading.Event().wait(timeout=2.5)
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)

    assert sync_mock.call_count >= 2


def test_run_decision_pipeline_aborts_remaining_symbols_after_kill_switch(
    heartbeat_file: Path, db_session: Session
) -> None:
    """Regresion (PR #108, finding A): un kill switch persistido mientras se
    procesa un simbolo debe frenar los simbolos siguientes del mismo tick, no
    solo la proxima vuelta del while — antes, _sync_state_from_db solo corria
    entre iteraciones y el pipeline podia seguir abriendo posiciones para el
    resto de los simbolos aunque la API ya mostrara KILL_SWITCH_TRIGGERED."""
    bot_run = _make_bot_run(db_session)
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        session=db_session,
        bot_run_id=bot_run.id,
    )

    processed: list[str] = []

    async def fake_process_symbol(snapshot: Mock) -> None:
        processed.append(snapshot.symbol)
        # Simula el kill switch disparado desde la API mientras este simbolo
        # estaba "en medio de su llamada a GPT".
        db_session.add(
            BotStateRow(
                bot_run_id=bot_run.id,
                state="KILL_SWITCH_TRIGGERED",
                previous_state="ACTIVE",
                reason="kill switch manual",
            )
        )
        db_session.commit()

    runner._process_symbol = fake_process_symbol  # type: ignore[method-assign]

    snapshots = [Mock(symbol="BTCUSDT"), Mock(symbol="ETHUSDT")]
    asyncio.run(runner._run_decision_pipeline(snapshots))  # type: ignore[attr-defined]

    assert processed == ["BTCUSDT"]
    assert sm.state == BotState.KILL_SWITCH_TRIGGERED


def test_run_decision_pipeline_skips_new_entries_in_safe_mode_but_keeps_looping(
    heartbeat_file: Path, db_session: Session
) -> None:
    """F16 [115]: SAFE_MODE administra posiciones existentes pero no abre nuevas
    (BotStateMachine.can_trade()). A diferencia de is_running()==False (que aborta
    el resto del tick), SAFE_MODE debe seguir evaluando los simbolos siguientes —
    is_running() sigue True, asi que PositionTickService debe seguir gestionando
    salidas del resto de simbolos en el mismo tick."""
    bot_run = _make_bot_run(db_session)
    db_session.add(
        BotStateRow(
            bot_run_id=bot_run.id,
            state="SAFE_MODE",
            previous_state="ACTIVE",
            reason="ordenes huerfanas detectadas",
        )
    )
    db_session.commit()
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        session=db_session,
        bot_run_id=bot_run.id,
    )

    processed: list[str] = []

    async def fake_process_symbol(snapshot: Mock) -> None:
        processed.append(snapshot.symbol)

    runner._process_symbol = fake_process_symbol  # type: ignore[method-assign]

    sync_calls = 0
    original_sync = runner._sync_state_from_db

    def spy_sync() -> None:
        nonlocal sync_calls
        sync_calls += 1
        original_sync()

    runner._sync_state_from_db = spy_sync  # type: ignore[method-assign]

    snapshots = [Mock(symbol="BTCUSDT"), Mock(symbol="ETHUSDT")]
    asyncio.run(runner._run_decision_pipeline(snapshots))  # type: ignore[attr-defined]

    assert processed == []
    assert sm.state == BotState.SAFE_MODE
    # Se resincronizo y evaluo cada simbolo (no aborto tras el primero, a
    # diferencia del caso KILL_SWITCH_TRIGGERED de arriba).
    assert sync_calls == 2


# ---------------------------------------------------------------------------
# Slippage pre-trade en _process_symbol (F17 [162], regla no negociable 13)
# ---------------------------------------------------------------------------


_SLIP_BID = Decimal("49990")
_SLIP_ASK = Decimal("50010")
_SLIP_ENTRY = 50000.0


def _slippage_candle() -> CandleData:
    return CandleData(
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50000"),
        volume=Decimal("100"),
        n_candles=10,
    )


def _slippage_snapshot() -> MarketSnapshot:
    now = datetime.now(UTC)
    candle = _slippage_candle()
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.BINGX,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        bid=_SLIP_BID,
        ask=_SLIP_ASK,
        spread_absolute=_SLIP_ASK - _SLIP_BID,
        spread_percent=(_SLIP_ASK - _SLIP_BID) / _SLIP_BID * 100,
        candles=Candles(tf_5m=candle, tf_15m=candle, tf_1h=candle, tf_4h=candle),
        volume=Decimal("1000"),
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=now,
        local_time=now,
        clock_skew_ms=10,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _slippage_decision(margin_usdt: float = 5.0, leverage: int = 3) -> ModelDecision:
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=datetime.now(UTC),
        decision=DecisionType.LONG,
        symbol="BTCUSDT",
        entry_type=EntryType.MARKET,
        entry_price=_SLIP_ENTRY,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        invalidation_price=48_500.0,
        leverage=leverage,
        margin_usdt=margin_usdt,
        estimated_notional_usdt=margin_usdt * leverage,
        estimated_entry_fee_usdt=0.05,
        estimated_exit_fee_usdt=0.05,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.0,
        estimated_max_loss_usdt=5.0,
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
        ),
        news_context=NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary="No news."),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="fixture de slippage",
        execute=True,
    )


def _slippage_runner(
    heartbeat_file: Path,
    db_session: Session,
    decision,
    risk_decision: RiskDecision,
    adjusted: AdjustedParameters | None,
) -> tuple[CycleRunner, Mock]:
    """CycleRunner con todo mockeado salvo el cálculo de slippage, que es el sujeto."""
    config = load_config()
    execution_engine = Mock(spec=ExecutionEngine)
    execution_engine.get_open_position_unrealized_pnl.return_value = None

    gpt_client = Mock()

    async def _request(*_args: object, **_kwargs: object):
        return decision

    gpt_client.request = _request

    aggregation = DecisionAggregationResult(
        decision_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=decision.timestamp_utc,
        contributing_sources=ContributingSources(
            quant_score=0.80,
            gpt_context_score=0.85,
            regime_factor=0.75,
            volatility_factor=0.70,
        ),
        aggregated_score=0.78,
        final_action=DecisionType.LONG,
    )
    aggregator = Mock()
    aggregator.aggregate.return_value = aggregation

    prompt_builder = Mock()
    prompt_builder.build.return_value = ("system", "user")

    trade_repo = Mock()
    trade_repo.get_loss_totals.return_value = (Decimal("0"), Decimal("0"))
    trade_repo.get_last_closed_trade.return_value = None

    risk_result = RiskValidationResult(
        aggregation_id=aggregation.aggregation_id,
        symbol=decision.symbol,
        timestamp_utc=datetime.now(UTC),
        decision=risk_decision,
        original_margin_usdt=Decimal(str(decision.margin_usdt)),
        original_leverage=decision.leverage,
        adjusted_parameters=adjusted,
        reasons={"test": "fixture"},
    )

    runner = CycleRunner(
        BotStateMachine(initial=BotState.ACTIVE),
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        execution_engine=execution_engine,
    )
    runner._config = config  # type: ignore[attr-defined]
    runner._session = db_session  # type: ignore[attr-defined]
    runner._bot_run_id = str(uuid.uuid4())  # type: ignore[attr-defined]
    runner._gpt_client = gpt_client  # type: ignore[attr-defined]
    runner._prompt_builder = prompt_builder  # type: ignore[attr-defined]
    runner._aggregator = aggregator  # type: ignore[attr-defined]
    runner._regime_engine = MarketRegimeEngine()  # type: ignore[attr-defined]
    runner._trade_repo = trade_repo  # type: ignore[attr-defined]
    runner._risk_result_for_test = risk_result  # type: ignore[attr-defined]
    return runner, execution_engine


def test_process_symbol_persists_estimate_for_the_adjusted_notional(
    heartbeat_file: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tras un ADJUST_DOWN se persiste el estimado del notional ejecutado, no del propuesto.

    Es el número que después se compara contra el slippage real de la misma
    fila de `orders`: si describiera la orden que se pidió en vez de la que se
    colocó, la comparación estimado-vs-real mentiría por el factor del ajuste.
    """
    decision = _slippage_decision(margin_usdt=10.0, leverage=4)
    adjusted = AdjustedParameters(margin_usdt=Decimal("5"), leverage=2)
    runner, execution_engine = _slippage_runner(
        heartbeat_file, db_session, decision, RiskDecision.ADJUST_DOWN, adjusted
    )
    monkeypatch.setattr(
        risk_engine,
        "validate",
        lambda **_kw: runner._risk_result_for_test,  # type: ignore[attr-defined]
    )

    asyncio.run(runner._process_symbol(_slippage_snapshot()))  # type: ignore[attr-defined]

    execution_engine.execute_approved_plan.assert_called_once()
    persisted = execution_engine.execute_approved_plan.call_args.kwargs["slippage_estimate"]
    expected = estimate_for_decision(
        snapshot=_slippage_snapshot(),
        decision=decision,
        margin_usdt=adjusted.margin_usdt,
        leverage=adjusted.leverage,
        market_impact_bps=Decimal(str(load_config().slippage.market_impact_bps)),
    )
    assert persisted.estimated_slippage_usdt == expected.estimated_slippage_usdt
    # Y no el del notional propuesto: 10 × 4 = 40 contra 5 × 2 = 10, 4x más grande.
    proposed = estimate_for_decision(
        snapshot=_slippage_snapshot(),
        decision=decision,
        margin_usdt=Decimal("10"),
        leverage=4,
        market_impact_bps=Decimal(str(load_config().slippage.market_impact_bps)),
    )
    assert proposed.estimated_slippage_usdt == expected.estimated_slippage_usdt * 4
    assert persisted.estimated_slippage_usdt != proposed.estimated_slippage_usdt


def test_process_symbol_passes_proposed_estimate_to_risk_engine(
    heartbeat_file: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El Risk Engine recibe el estimado *propuesto*: es lo único que existe pre-trade."""
    decision = _slippage_decision(margin_usdt=10.0, leverage=4)
    adjusted = AdjustedParameters(margin_usdt=Decimal("5"), leverage=2)
    runner, _engine = _slippage_runner(
        heartbeat_file, db_session, decision, RiskDecision.ADJUST_DOWN, adjusted
    )
    seen: dict[str, object] = {}

    def _capture(**kwargs: object):
        seen.update(kwargs)
        return runner._risk_result_for_test  # type: ignore[attr-defined]

    monkeypatch.setattr(risk_engine, "validate", _capture)

    asyncio.run(runner._process_symbol(_slippage_snapshot()))  # type: ignore[attr-defined]

    proposed = estimate_for_decision(
        snapshot=_slippage_snapshot(),
        decision=decision,
        margin_usdt=Decimal("10"),
        leverage=4,
        market_impact_bps=Decimal(str(load_config().slippage.market_impact_bps)),
    )
    estimate = seen["slippage_estimate"]
    assert estimate is not None
    assert estimate.estimated_slippage_usdt == proposed.estimated_slippage_usdt


def test_process_symbol_skips_estimate_for_non_executable_decision(
    heartbeat_file: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute=False no puede reventar el ciclo: margin y entry_price valen 0 por schema."""
    decision = _slippage_decision().model_copy(
        update={"execute": False, "margin_usdt": 0.0, "entry_price": 0.0}
    )
    runner, execution_engine = _slippage_runner(
        heartbeat_file, db_session, decision, RiskDecision.NO_OPERAR, None
    )
    seen: dict[str, object] = {}

    def _capture(**kwargs: object):
        seen.update(kwargs)
        return runner._risk_result_for_test  # type: ignore[attr-defined]

    monkeypatch.setattr(risk_engine, "validate", _capture)

    asyncio.run(runner._process_symbol(_slippage_snapshot()))  # type: ignore[attr-defined]

    assert seen["slippage_estimate"] is None
    execution_engine.execute_approved_plan.assert_not_called()
