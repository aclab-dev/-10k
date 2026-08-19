"""Tests para worker/run_worker.py: parseo de backoff y el camino de error de main()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from worker.run_worker import (
    DEFAULT_STARTUP_RACE_BACKOFF_SECONDS,
    main,
    parse_backoff_seconds_from_env,
)

# -- parse_backoff_seconds_from_env --


def test_parse_backoff_default_when_none() -> None:
    assert parse_backoff_seconds_from_env(None) == DEFAULT_STARTUP_RACE_BACKOFF_SECONDS


def test_parse_backoff_default_when_empty() -> None:
    assert parse_backoff_seconds_from_env("") == DEFAULT_STARTUP_RACE_BACKOFF_SECONDS


def test_parse_backoff_valid_int() -> None:
    assert parse_backoff_seconds_from_env("60") == 60


def test_parse_backoff_zero_is_valid() -> None:
    """0 es válido (sin espera) — a diferencia del intervalo de heartbeat, acá
    no hay división ni un mínimo operativo que lo impida."""
    assert parse_backoff_seconds_from_env("0") == 0


def test_parse_backoff_non_int_raises() -> None:
    with pytest.raises(ValueError, match="must be an int"):
        parse_backoff_seconds_from_env("abc")


def test_parse_backoff_negative_raises() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        parse_backoff_seconds_from_env("-5")


# -- main(): camino de BotRunAlreadyActiveError --


def test_main_sleeps_backoff_then_exits_on_bot_run_already_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perder la carrera de arranque duerme el backoff configurado y sale con
    código 1 — no debe propagar la excepción cruda ni seguir a install_signal_handlers/run()."""
    from backend.trading_core.orchestrator import BotRunAlreadyActiveError

    monkeypatch.setenv("WORKER_STARTUP_RACE_BACKOFF_SECONDS", "42")

    with (
        patch("worker.run_worker.configure_logging"),
        patch(
            "worker.run_worker.Orchestrator", side_effect=BotRunAlreadyActiveError("boom")
        ) as mock_orchestrator_cls,
        patch("worker.run_worker.time.sleep") as mock_sleep,
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    assert excinfo.value.code == 1
    mock_sleep.assert_called_once_with(42)
    mock_orchestrator_cls.assert_called_once_with()


def test_main_runs_normally_without_bot_run_already_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Camino feliz: no debe dormir ni salir — instala señales y corre."""
    monkeypatch.delenv("WORKER_STARTUP_RACE_BACKOFF_SECONDS", raising=False)

    mock_orchestrator = MagicMock()

    with (
        patch("worker.run_worker.configure_logging"),
        patch("worker.run_worker.Orchestrator", return_value=mock_orchestrator),
        patch("worker.run_worker.time.sleep") as mock_sleep,
    ):
        main()

    mock_sleep.assert_not_called()
    mock_orchestrator.install_signal_handlers.assert_called_once()
    mock_orchestrator.run.assert_called_once()
