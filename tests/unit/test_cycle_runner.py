"""Tests del CycleRunner."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.position_manager.tick_service import PositionTickService
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import (
    DEFAULT_INTERVAL_SECONDS,
    CycleRunner,
    parse_interval_from_env,
)


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
