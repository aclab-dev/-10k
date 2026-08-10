"""Tests del CycleRunner."""

from __future__ import annotations

import threading
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.execution.engine import ExecutionEngine
from backend.market_data.cycle_service import MarketDataCycleService
from backend.position_manager.tick_service import PositionTickService
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


def test_run_skips_tick_when_state_machine_not_running(heartbeat_file: Path) -> None:
    """En estado HALTED el runner sigue vivo pero no hace heartbeat."""
    sm = BotStateMachine(initial=BotState.HALTED)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        # Dar tiempo a que itere al menos una vez.
        threading.Event().wait(timeout=1.5)
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)

    # No se debe tocar el heartbeat porque el state no esta running.
    assert not heartbeat_file.exists()


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


def test_run_calls_sync_state_from_db_each_iteration(heartbeat_file: Path) -> None:
    """Regresion: run() debe releer el estado persistido en cada vuelta del loop,

    no solo al construir el runner."""
    sm = BotStateMachine(initial=BotState.ACTIVE)
    runner = CycleRunner(sm, interval_seconds=1, heartbeat_file=heartbeat_file)
    sync_mock = Mock()
    runner._sync_state_from_db = sync_mock  # type: ignore[method-assign]

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        threading.Event().wait(timeout=1.5)
    finally:
        runner.request_shutdown()
        thread.join(timeout=3.0)

    assert sync_mock.call_count >= 1
