"""Tests del worker placeholder."""

import signal
from pathlib import Path

import pytest

from worker import run_worker


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Asegura que _shutdown empieza en False y se resetea entre tests."""
    run_worker._shutdown = False
    yield
    run_worker._shutdown = False


def test_signal_handler_sets_shutdown_flag() -> None:
    """SIGTERM debe marcar _shutdown=True para que el loop pueda salir."""
    assert run_worker._shutdown is False
    run_worker._handle_signal(signal.SIGTERM, None)
    assert run_worker._shutdown is True


def test_signal_handler_works_with_sigint() -> None:
    """SIGINT (Ctrl+C) tambien debe marcar shutdown."""
    assert run_worker._shutdown is False
    run_worker._handle_signal(signal.SIGINT, None)
    assert run_worker._shutdown is True


def test_main_touches_heartbeat_and_exits_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """El loop debe hacer touch del archivo de heartbeat y terminar cuando _shutdown=True."""
    touched: list[Path] = []

    def fake_touch(self: Path, exist_ok: bool = False) -> None:
        touched.append(self)
        # Salir del loop tras el primer heartbeat para que el test no se cuelgue.
        run_worker._shutdown = True

    def fake_sleep(_seconds: float) -> None:
        # Evita que el test tarde lo del intervalo real.
        pass

    monkeypatch.setattr(Path, "touch", fake_touch)
    monkeypatch.setattr(run_worker.time, "sleep", fake_sleep)
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("ENVIRONMENT", "PAPER")

    run_worker.main()

    assert len(touched) == 1
    assert touched[0] == run_worker.HEARTBEAT_FILE


def test_main_uses_default_interval_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin WORKER_HEARTBEAT_INTERVAL_SECONDS, debe usar el default."""
    monkeypatch.delenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", raising=False)

    def fake_touch(self: Path, exist_ok: bool = False) -> None:
        run_worker._shutdown = True

    monkeypatch.setattr(Path, "touch", fake_touch)
    monkeypatch.setattr(run_worker.time, "sleep", lambda _s: None)

    # No debe lanzar excepcion al parsear el default.
    run_worker.main()

    assert run_worker.DEFAULT_INTERVAL_SECONDS == 10
