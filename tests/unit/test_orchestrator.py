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
from backend.position_manager.tick_service import PositionTickService
from backend.storage.database import Base
from backend.storage.models import BotRun
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories.bot import BotRunRepository, BotStateRepository
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


def test_default_construction_wires_paper_position_tick_service(sqlite_session: Session) -> None:
    """Sin nada inyectado, arma un PositionTickService real (F14) atado al mismo
    PositionManager que usa ExecutionEngine, con mark_price real via el cache
    de MarketDataCycleService."""
    orch = Orchestrator(session=sqlite_session)

    pts = orch.cycle_runner._position_tick_service  # type: ignore[attr-defined]
    assert isinstance(pts, PositionTickService)
    assert pts._pm is orch.position_manager  # type: ignore[attr-defined]

    mds = orch.cycle_runner._market_data_service  # type: ignore[attr-defined]
    mds.tick_all()

    # get_mark_price debe resolver al ultimo precio cacheado por el tick de market data.
    price = pts._get_mark_price("BTCUSDT")  # type: ignore[attr-defined]
    assert price == mds.get_last_price("BTCUSDT")


def test_position_tick_service_mark_price_raises_before_first_market_data_tick(
    sqlite_session: Session,
) -> None:
    """Sin ningun tick de market data corrido aun, get_mark_price debe fallar
    explicito (LookupError) en vez de devolver un precio inexistente."""
    orch = Orchestrator(session=sqlite_session)

    pts = orch.cycle_runner._position_tick_service  # type: ignore[attr-defined]
    with pytest.raises(LookupError, match="BTCUSDT"):
        pts._get_mark_price("BTCUSDT")  # type: ignore[attr-defined]


def test_position_tick_service_is_none_when_market_data_service_injected() -> None:
    """Si el caller inyecta market_data_service/execution_engine, es dueno de ese
    ciclo de vida — Orchestrator no arma un PositionTickService propio."""
    orch = Orchestrator(
        market_data_service=Mock(spec=MarketDataCycleService),
        execution_engine=Mock(spec=ExecutionEngine),
    )
    assert orch.cycle_runner._position_tick_service is None  # type: ignore[attr-defined]


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


def test_worker_restart_carries_over_kill_switch(sqlite_session: Session) -> None:
    """Si el ultimo bot_run quedo en KILL_SWITCH_TRIGGERED, el worker que arranca
    despues (restart) debe heredar ese estado en vez de arrancar en ACTIVE.

    Regresion del hallazgo G de la re-review de Agustin en el PR #108: bot_state
    esta scopeado por bot_run_id, y cada arranque crea un bot_run nuevo — sin
    este carry-over, un `docker compose restart` despues de un kill switch
    reactiva el bot silenciosamente.
    """
    first = Orchestrator(session=sqlite_session)
    first_bot_run_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    BotStateRepository(sqlite_session).save(
        BotStateRow(
            bot_run_id=first_bot_run_id,
            state=BotState.KILL_SWITCH_TRIGGERED.value,
            previous_state=BotState.ACTIVE.value,
            reason="operador aprieta el boton",
        )
    )
    sqlite_session.commit()
    first.close()  # simula el shutdown limpio (SIGTERM) antes del restart

    second = Orchestrator(session=sqlite_session)

    assert second.state_machine.state == BotState.KILL_SWITCH_TRIGGERED
    assert second.cycle_runner._state_machine.is_running() is False  # type: ignore[attr-defined]

    second_bot_run_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]
    persisted = BotStateRepository(sqlite_session).get_latest(second_bot_run_id)
    assert persisted is not None
    assert persisted.state == BotState.KILL_SWITCH_TRIGGERED.value


def test_worker_restart_stays_active_without_prior_kill_switch(sqlite_session: Session) -> None:
    """Sin kill switch previo, un restart arranca en ACTIVE como siempre."""
    first = Orchestrator(session=sqlite_session)
    first.close()

    second = Orchestrator(session=sqlite_session)

    assert second.state_machine.state == BotState.ACTIVE
    second_bot_run_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]
    assert BotStateRepository(sqlite_session).get_latest(second_bot_run_id) is None


def test_worker_restart_carries_over_halted(sqlite_session: Session) -> None:
    """HALTED tambien detiene el ciclo (is_running() == False), asi que debe
    arrastrarse igual que KILL_SWITCH_TRIGGERED.

    Regresion del hallazgo J de la re-review de Agustin en el PR #108: comparar
    contra KILL_SWITCH_TRIGGERED puntualmente deja este agujero para cualquier
    otro estado que frene el ciclo.
    """
    first = Orchestrator(session=sqlite_session)
    first_bot_run_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    BotStateRepository(sqlite_session).save(
        BotStateRow(
            bot_run_id=first_bot_run_id,
            state=BotState.HALTED.value,
            previous_state=BotState.ACTIVE.value,
            reason="risk engine",
        )
    )
    sqlite_session.commit()
    first.close()

    second = Orchestrator(session=sqlite_session)

    assert second.state_machine.state == BotState.HALTED
    assert second.cycle_runner._state_machine.is_running() is False  # type: ignore[attr-defined]

    second_bot_run_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]
    persisted = BotStateRepository(sqlite_session).get_latest(second_bot_run_id)
    assert persisted is not None
    assert persisted.state == BotState.HALTED.value


def test_carry_over_and_new_bot_run_share_a_single_commit(sqlite_session: Session) -> None:
    """El bot_run nuevo y su bot_state arrastrado deben nacer atomicamente.

    Regresion del hallazgo I de la re-review de Agustin en el PR #108: dos
    commits separados dejan una ventana en la que el bot_run nuevo ya existe
    en la DB pero todavia no tiene bot_state, y un crash justo ahi hace que el
    proximo arranque no encuentre nada que arrastrar y quede en ACTIVE. Un
    solo commit para ambos cierra esa ventana.
    """
    first = Orchestrator(session=sqlite_session)
    first_bot_run_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    BotStateRepository(sqlite_session).save(
        BotStateRow(
            bot_run_id=first_bot_run_id,
            state=BotState.KILL_SWITCH_TRIGGERED.value,
            previous_state=BotState.ACTIVE.value,
            reason="operador aprieta el boton",
        )
    )
    sqlite_session.commit()
    first.close()

    commit_calls = 0
    original_commit = sqlite_session.commit

    def counting_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        original_commit()

    sqlite_session.commit = counting_commit  # type: ignore[method-assign]

    second = Orchestrator(session=sqlite_session)

    assert commit_calls == 1
    second_bot_run_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]
    persisted = BotStateRepository(sqlite_session).get_latest(second_bot_run_id)
    assert persisted is not None
    assert persisted.state == BotState.KILL_SWITCH_TRIGGERED.value


def _simulate_sigkill(orch: Orchestrator) -> None:
    """Deja el BotRun colgado en RUNNING, como cuando el proceso muere por SIGKILL.

    close() (que corre en el finally de run()) es justamente lo que no pasa en
    ese caso. Solo se libera el thread pool del ExecutionEngine, que es del
    proceso de pytest y no de la corrida simulada.
    """
    if orch.execution_engine is not None:
        orch.execution_engine.close()


def test_startup_closes_orphan_running_bot_run(sqlite_session: Session) -> None:
    """Un bot_run que quedo en RUNNING sin shutdown limpio se cierra como CRASHED
    al arrancar el worker siguiente, y el activo pasa a ser el nuevo.

    Sin esto conviven dos filas RUNNING: la API resuelve el bot_run por
    get_active() y el worker sincroniza contra el suyo, asi que el kill switch
    puede escribir el estado de una corrida que ya no existe.
    """
    first = Orchestrator(session=sqlite_session)
    orphan_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    _simulate_sigkill(first)

    second = Orchestrator(session=sqlite_session)
    second_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]

    repo = BotRunRepository(sqlite_session)
    orphan = repo.get_by_id(orphan_id)
    assert orphan is not None
    assert orphan.status == "CRASHED"
    assert orphan.ended_at is not None
    assert orphan.notes is not None and "SIGKILL" in orphan.notes

    active = repo.get_active()
    assert active is not None
    assert active.id == second_id


def test_prepare_paper_context_raises_when_bot_run_insert_violates_running_constraint(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El IntegrityError de uq_bot_runs_single_running (F16 [114]) al insertar el
    BotRun nuevo debe traducirse a BotRunAlreadyActiveError explícito, no propagar
    crudo. El disparo real de la constraint bajo concurrencia se valida contra
    Postgres real en tests/integration/test_bot_run_concurrency.py (SQLite es de
    una sola conexión, no sirve para simular dos procesos corriendo a la vez);
    acá se mockea puntualmente el flush que persiste el BotRun nuevo para aislar
    la lógica de traducción de la excepción.
    """
    from sqlalchemy.exc import IntegrityError

    from backend.storage.models import BotRun
    from backend.trading_core.orchestrator import BotRunAlreadyActiveError

    original_flush = sqlite_session.flush

    def _flush_raising_for_new_bot_run() -> None:
        if any(isinstance(obj, BotRun) for obj in sqlite_session.new):
            raise IntegrityError(
                "INSERT INTO bot_runs ...",
                {},
                Exception("UNIQUE constraint failed: bot_runs.status"),
            )
        original_flush()

    monkeypatch.setattr(sqlite_session, "flush", _flush_raising_for_new_bot_run)

    with pytest.raises(BotRunAlreadyActiveError):
        Orchestrator(session=sqlite_session)


def test_startup_carries_over_kill_switch_from_orphan_run(sqlite_session: Session) -> None:
    """Cerrar el huerfano no debe romper el carry-over: el estado se arrastra por
    bot_state del run mas reciente, no por su status."""
    first = Orchestrator(session=sqlite_session)
    orphan_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    BotStateRepository(sqlite_session).save(
        BotStateRow(
            bot_run_id=orphan_id,
            state=BotState.KILL_SWITCH_TRIGGERED.value,
            previous_state=BotState.ACTIVE.value,
            reason="operador aprieta el boton",
        )
    )
    sqlite_session.commit()
    _simulate_sigkill(first)

    second = Orchestrator(session=sqlite_session)

    assert second.state_machine.state == BotState.KILL_SWITCH_TRIGGERED
    second_id = second._bot_run.id  # type: ignore[attr-defined,union-attr]
    persisted = BotStateRepository(sqlite_session).get_latest(second_id)
    assert persisted is not None
    assert persisted.state == BotState.KILL_SWITCH_TRIGGERED.value

    orphan = BotRunRepository(sqlite_session).get_by_id(orphan_id)
    assert orphan is not None
    assert orphan.status == "CRASHED"


def test_startup_does_not_touch_runs_closed_cleanly(sqlite_session: Session) -> None:
    """Un run cerrado con shutdown limpio conserva STOPPED: CRASHED es la marca
    de que el proceso murio mal, y sirve solo si distingue los dos casos."""
    first = Orchestrator(session=sqlite_session)
    first_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    first.close()

    Orchestrator(session=sqlite_session)

    closed = BotRunRepository(sqlite_session).get_by_id(first_id)
    assert closed is not None
    assert closed.status == "STOPPED"
    assert closed.notes is None


def test_injected_state_machine_does_not_carry_over_kill_switch(sqlite_session: Session) -> None:
    """Si el caller inyecta su propia state machine, es dueno del estado inicial:
    el Orchestrator no debe pisarlo con un carry-over."""
    first = Orchestrator(session=sqlite_session)
    first_bot_run_id = first._bot_run.id  # type: ignore[attr-defined,union-attr]
    BotStateRepository(sqlite_session).save(
        BotStateRow(
            bot_run_id=first_bot_run_id,
            state=BotState.KILL_SWITCH_TRIGGERED.value,
            previous_state=BotState.ACTIVE.value,
            reason="operador aprieta el boton",
        )
    )
    sqlite_session.commit()
    first.close()

    sm = BotStateMachine(initial=BotState.ACTIVE)
    second = Orchestrator(
        state_machine=sm,
        market_data_service=Mock(spec=MarketDataCycleService),
        execution_engine=Mock(spec=ExecutionEngine),
    )

    assert second.state_machine.state == BotState.ACTIVE


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
