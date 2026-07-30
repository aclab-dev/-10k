"""Tests del Orchestrator."""

from __future__ import annotations

import signal
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Environment
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.execution.engine import ExecutionEngine
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.fetcher import MockDataFetcher
from backend.position_manager.manager import PositionManager
from backend.storage.database import Base
from backend.storage.models import BotRun
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner
from backend.trading_core.orchestrator import Orchestrator


@pytest.fixture
def heartbeat_file(tmp_path: Path) -> Path:
    return tmp_path / "worker_alive"


@pytest.fixture
def sqlite_session() -> Generator[Session, None, None]:
    """Sesion in-memory (SQLite) como test double liviano — ver PgJSON en database.py."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()


def test_default_construction_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator() sin args debe leer la env var y armar la CycleRunner."""
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "25")
    orch = Orchestrator(
        market_data_service=Mock(spec=MarketDataCycleService),
        execution_engine=Mock(spec=ExecutionEngine),
    )
    assert orch.state_machine.state == BotState.ACTIVE
    # Lo que verdaderamente queremos verificar: que el env se haya parseado.
    assert orch.cycle_runner.interval_seconds == 25


def test_default_construction_uses_default_interval_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin la env var, debe caer al default declarado en cycle_runner."""
    from backend.trading_core.cycle_runner import DEFAULT_INTERVAL_SECONDS

    monkeypatch.delenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", raising=False)
    orch = Orchestrator(
        market_data_service=Mock(spec=MarketDataCycleService),
        execution_engine=Mock(spec=ExecutionEngine),
    )
    assert orch.cycle_runner.interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_default_construction_wires_paper_market_data_pipeline(sqlite_session: Session) -> None:
    """Sin market_data_service inyectado, arma el pipeline real PAPER (CR)."""
    orch = Orchestrator(session=sqlite_session)

    mds = orch.cycle_runner._market_data_service  # type: ignore[attr-defined]
    assert isinstance(mds, MarketDataCycleService)
    assert isinstance(mds._adapter, PaperAdapter)  # type: ignore[attr-defined]
    assert isinstance(mds._fetcher, MockDataFetcher)  # type: ignore[attr-defined]
    assert mds._symbols == ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]  # type: ignore[attr-defined]
    # El balance inicial del PaperAdapter viene de config.yaml (challenge.initial_balance_usdt),
    # no de un default hardcodeado.
    assert mds._adapter.get_account_state().balance_usdt == Decimal("100")  # type: ignore[attr-defined]

    bot_runs = sqlite_session.query(BotRun).all()
    assert len(bot_runs) == 1
    assert bot_runs[0].environment == "PAPER"
    assert bot_runs[0].status == "RUNNING"


def test_default_construction_wires_paper_execution_pipeline(sqlite_session: Session) -> None:
    """Sin execution_engine inyectado, arma ExecutionEngine + PositionManager reales (CR)."""
    orch = Orchestrator(session=sqlite_session)

    exec_engine = orch.cycle_runner.execution_engine
    assert isinstance(exec_engine, ExecutionEngine)
    assert orch.execution_engine is exec_engine
    assert isinstance(orch.position_manager, PositionManager)
    assert exec_engine._position_manager is orch.position_manager  # type: ignore[attr-defined]

    # Mismo PaperAdapter que market data — no dos instancias con estado divergente.
    mds = orch.cycle_runner._market_data_service  # type: ignore[attr-defined]
    assert exec_engine._adapter is mds._adapter  # type: ignore[attr-defined]


def test_only_market_data_service_injected_raises() -> None:
    """Inyectar solo market_data_service construiria un ExecutionEngine real con un

    segundo PaperAdapter divergente del que ya arma market_data_service — debe
    fallar explícito, no silencioso.
    """
    with pytest.raises(ValueError, match="deben inyectarse juntos"):
        Orchestrator(market_data_service=Mock(spec=MarketDataCycleService))


def test_only_execution_engine_injected_raises() -> None:
    """Inverso: solo execution_engine tambien debe fallar explícito."""
    with pytest.raises(ValueError, match="deben inyectarse juntos"):
        Orchestrator(execution_engine=Mock(spec=ExecutionEngine))


def test_run_closes_bot_run_on_graceful_shutdown(sqlite_session: Session) -> None:
    """run() debe cerrar (STOPPED) el BotRun propio al terminar, no dejarlo RUNNING colgado."""
    orch = Orchestrator(session=sqlite_session)
    orch.cycle_runner.request_shutdown()  # el loop no debe ejecutar ningun tick

    orch.run()

    bot_run = sqlite_session.query(BotRun).one()
    assert bot_run.status == "STOPPED"
    assert bot_run.ended_at is not None


def test_run_does_not_touch_bot_run_when_cycle_runner_injected(heartbeat_file: Path) -> None:
    """Si cycle_runner viene inyectado, este Orchestrator no creo ningun BotRun propio."""
    sm = BotStateMachine()
    runner = CycleRunner(sm, interval_seconds=60, heartbeat_file=heartbeat_file)
    runner.request_shutdown()
    orch = Orchestrator(state_machine=sm, cycle_runner=runner)

    orch.run()  # no debe lanzar ni intentar cerrar un BotRun inexistente


def test_run_closes_bot_run_even_when_cycle_runner_raises(sqlite_session: Session) -> None:
    """El cierre debe ocurrir aunque CycleRunner.run() levante (finally, no solo shutdown OK)."""
    orch = Orchestrator(session=sqlite_session)
    orch._cycle_runner = Mock(run=Mock(side_effect=RuntimeError("boom")))  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="boom"):
        orch.run()

    bot_run = sqlite_session.query(BotRun).one()
    assert bot_run.status == "STOPPED"


def test_close_is_public_and_idempotent(sqlite_session: Session) -> None:
    """close() es invocable sin pasar por run() (scripts/tooling), y no falla si se repite."""
    orch = Orchestrator(session=sqlite_session)

    orch.close()
    bot_run = sqlite_session.query(BotRun).one()
    assert bot_run.status == "STOPPED"

    orch.close()  # no debe lanzar ni re-tocar un BotRun ya cerrado


def test_default_construction_raises_for_non_paper_environment(
    monkeypatch: pytest.MonkeyPatch, sqlite_session: Session
) -> None:
    """TESTNET/LIVE no estan wireados aun (F16/F17) — debe fallar rapido, no silencioso."""
    from backend.trading_core import orchestrator as orchestrator_module

    fake_cfg = Mock()
    fake_cfg.execution.environment = Environment.TESTNET
    monkeypatch.setattr(orchestrator_module, "get_config", lambda: fake_cfg)

    with pytest.raises(NotImplementedError, match="TESTNET"):
        Orchestrator(session=sqlite_session)


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
