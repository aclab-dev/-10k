"""Tests del Orchestrator."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner
from backend.trading_core.orchestrator import Orchestrator


@pytest.fixture
def heartbeat_file(tmp_path: Path) -> Path:
    return tmp_path / "worker_alive"


def test_default_construction_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator() sin args debe leer la env var y armar la CycleRunner."""
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "25")
    orch = Orchestrator()
    assert orch.state_machine.state == BotState.ACTIVE
    # Lo que verdaderamente queremos verificar: que el env se haya parseado.
    assert orch.cycle_runner.interval_seconds == 25


def test_default_construction_uses_default_interval_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin la env var, debe caer al default declarado en cycle_runner."""
    from backend.trading_core.cycle_runner import DEFAULT_INTERVAL_SECONDS

    monkeypatch.delenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", raising=False)
    orch = Orchestrator()
    assert orch.cycle_runner.interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_construction_with_injected_deps(heartbeat_file: Path) -> None:
    sm = BotStateMachine(initial=BotState.SAFE_MODE)
    runner = CycleRunner(sm, interval_seconds=5, heartbeat_file=heartbeat_file)
    orch = Orchestrator(state_machine=sm, cycle_runner=runner)
    assert orch.state_machine is sm
    assert orch.cycle_runner is runner


def test_run_delegates_to_cycle_runner(heartbeat_file: Path) -> None:
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=60, heartbeat_file=heartbeat_file)
    runner.request_shutdown()  # Asegura que run() termine inmediatamente.
    orch = Orchestrator(state_machine=sm, cycle_runner=runner)
    orch.run()  # No debe colgar.


def test_signal_handler_triggers_shutdown(heartbeat_file: Path) -> None:
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=60, heartbeat_file=heartbeat_file)
    orch = Orchestrator(state_machine=sm, cycle_runner=runner)

    assert runner.shutdown_requested is False
    orch._handle_signal(signal.SIGTERM, None)  # type: ignore[attr-defined]
    assert runner.shutdown_requested is True


def test_install_signal_handlers_does_not_raise(
    heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifica que la instalacion de handlers no rompa, sin mutar globals."""
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=60, heartbeat_file=heartbeat_file)
    orch = Orchestrator(state_machine=sm, cycle_runner=runner)

    installed: list[tuple[int, object]] = []

    def fake_signal(signum: int, handler: object) -> object:
        installed.append((signum, handler))
        return None

    monkeypatch.setattr("backend.trading_core.orchestrator.signal.signal", fake_signal)
    orch.install_signal_handlers()
    sigs = [s for s, _ in installed]
    assert signal.SIGTERM in sigs
    assert signal.SIGINT in sigs
