"""Orchestrator: arma el grafo (state machine + cycle runner) y lo corre.

Es el punto de entrada logico del worker. Lee configuracion del entorno,
instancia los componentes en el orden correcto (incluyendo el ExchangeAdapter,
el Market Data Engine y el Execution Engine reales para PAPER) y bloquea
hasta que el loop termine (shutdown via signal o estado terminal).
"""

from __future__ import annotations

import os
import signal
from decimal import Decimal
from types import FrameType

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.core.config import APP_VERSION, AppConfig, Environment, get_config
from backend.decision_engine.aggregator import DecisionAggregator
from backend.decision_engine.gpt_client import GPTAuthError, GPTClient
from backend.decision_engine.prompt_builder import PromptBuilder
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.execution.engine import ExecutionEngine
from backend.market_data.analysis_service import MarketAnalysisService
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import MockDataFetcher
from backend.position_manager.manager import PositionManager
from backend.position_manager.tick_service import PositionTickService
from backend.reconciliation.engine import ReconciliationEngine
from backend.reconciliation.gate import ReconciliationGate
from backend.storage.database import get_session_factory
from backend.storage.models import BotRun
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories.bot import BotRunRepository, BotStateRepository
from backend.storage.repositories.trades import OrderRepository, PositionRepository
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner, parse_interval_from_env

log = structlog.get_logger(__name__)


class BotRunAlreadyActiveError(Exception):
    """Otro BotRun sigue RUNNING — el índice único parcial de DB rechazó el insert.

    Señala una race de arranque concurrente (F16 [114]), no un error transitorio:
    el caller no debe reintentar automáticamente, tiene que investigar por qué
    hay dos workers arrancando a la vez.
    """


class Orchestrator:
    """Coordina state machine + cycle runner.

    Se puede construir con dependencias inyectadas (para tests) o sin
    argumentos, en cuyo caso lee de variables de entorno/config y arma el
    pipeline real (PaperAdapter + MockDataFetcher + MarketAnalysisService +
    ExecutionEngine + PositionManager en PAPER).
    """

    def __init__(
        self,
        state_machine: BotStateMachine | None = None,
        cycle_runner: CycleRunner | None = None,
        market_data_service: MarketDataCycleService | None = None,
        execution_engine: ExecutionEngine | None = None,
        session: Session | None = None,
    ) -> None:
        # Si el caller no inyecta su propia state machine, este Orchestrator arma
        # el contexto PAPER real y es responsable de resolver un kill switch
        # arrastrado del BotRun anterior antes de decidir el estado inicial (ver
        # _resolve_carried_over_state). Un caller que inyecta su propia state
        # machine (tests, wiring alternativo) es dueño de ese estado inicial.
        self._owns_state_machine = state_machine is None
        self._state_machine = state_machine or BotStateMachine(initial=BotState.ACTIVE)
        # Solo se pueblan si este Orchestrator arma su propio contexto PAPER (ver
        # _prepare_paper_context). Si cycle_runner/market_data_service/
        # execution_engine vienen inyectados, el caller es dueño de ese ciclo de
        # vida, no nosotros.
        self._bot_run: BotRun | None = None
        self._db_session: Session | None = None
        self._position_manager: PositionManager | None = None
        self._execution_engine: ExecutionEngine | None = None
        if cycle_runner is None:
            interval = parse_interval_from_env(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS"))
            if (market_data_service is None) != (execution_engine is None):
                raise ValueError(
                    "market_data_service y execution_engine deben inyectarse juntos o "
                    "ninguno de los dos: comparten el mismo PaperAdapter/BotRun. Inyectar "
                    "solo uno construiria un segundo adapter con estado divergente."
                )
            if market_data_service is None:
                adapter, db_session, bot_run, cfg = self._prepare_paper_context(session)
                mds = self._build_market_data_service(adapter, db_session, bot_run, cfg)
                exec_engine = self._build_execution_engine(adapter, db_session, bot_run, cfg)
                assert self._position_manager is not None
                position_tick_service: PositionTickService | None = (
                    self._build_position_tick_service(mds, self._position_manager)
                )
                reconciliation_gate: ReconciliationGate | None = self._build_reconciliation_gate(
                    adapter, self._position_manager, db_session, bot_run, cfg
                )
                connection_health_monitor: ConnectionHealthMonitor | None = (
                    self._build_connection_health_monitor(db_session, bot_run, cfg)
                )
                gpt_client, prompt_builder, aggregator = self._build_decision_components(cfg)
                decision_session: Session | None = db_session
                decision_bot_run_id: str | None = str(bot_run.id)
                decision_cfg: AppConfig | None = cfg
            else:
                # execution_engine no puede ser None acá: si lo fuera, el check
                # XOR de arriba ya habría lanzado (market_data_service no es None
                # en esta rama). Narrowing explícito para mypy.
                assert execution_engine is not None
                mds = market_data_service
                exec_engine = execution_engine
                position_tick_service = None
                reconciliation_gate = None
                connection_health_monitor = None
                gpt_client, prompt_builder, aggregator = None, None, None
                decision_session = None
                decision_bot_run_id = None
                decision_cfg = None
            self._cycle_runner = CycleRunner(
                self._state_machine,
                interval_seconds=interval,
                position_tick_service=position_tick_service,
                reconciliation_gate=reconciliation_gate,
                connection_health_monitor=connection_health_monitor,
                market_data_service=mds,
                execution_engine=exec_engine,
                gpt_client=gpt_client,
                prompt_builder=prompt_builder,
                aggregator=aggregator,
                config=decision_cfg,
                session=decision_session,
                bot_run_id=decision_bot_run_id,
            )
        else:
            self._cycle_runner = cycle_runner
        self._signals_installed = False

    @staticmethod
    def _build_decision_components(
        cfg: AppConfig,
    ) -> tuple[GPTClient | None, PromptBuilder | None, DecisionAggregator | None]:
        """Intenta construir GPTClient, PromptBuilder y DecisionAggregator.

        Si OPENAI_API_KEY no esta disponible, loguea una advertencia y retorna
        (None, None, None). El CycleRunner omitira el pipeline de decision pero
        seguira corriendo market data y position management normalmente.
        """
        try:
            gpt_client = GPTClient(config=None, failure_policy=cfg.failure_policy)
        except GPTAuthError:
            log.warning(
                "orchestrator.gpt_client_unavailable",
                reason="OPENAI_API_KEY no configurada — pipeline de decision deshabilitado",
            )
            return None, None, None
        return gpt_client, PromptBuilder(), DecisionAggregator()

    def _prepare_paper_context(
        self, session: Session | None
    ) -> tuple[PaperAdapter, Session, BotRun, AppConfig]:
        """Arma, una sola vez, lo que market data y execution comparten: el mismo
        PaperAdapter (estado de cuenta/posiciones/órdenes debe ser uno solo),
        la sesión de DB y el BotRun del ciclo.

        Solo PAPER esta soportado: BingX real (TESTNET/LIVE) es F16/F17 y sigue
        bloqueado, asi que correr en otro environment falla rapido en vez de
        operar silenciosamente con PaperAdapter fuera de PAPER.

        Guarda bot_run/session en self para que run()/close() puedan cerrar el
        BotRun (status STOPPED) al hacer shutdown, evitando dejarlo colgado en
        RUNNING y rompiendo la semantica de BotRunRepository.get_active().
        """
        cfg = get_config()
        environment = cfg.execution.environment
        if environment != Environment.PAPER:
            raise NotImplementedError(
                f"ExchangeAdapter para environment={environment.value} no esta soportado aun "
                "(BingX real es F16/F17, sigue bloqueado). Solo PAPER esta wireado."
            )

        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        adapter = PaperAdapter(initial_balance_usdt=initial_balance)
        db_session = session or get_session_factory()()

        # Debe resolverse ANTES de crear el bot_run nuevo: get_most_recent()
        # busca el BotRun mas nuevo existente, que dejaria de ser el "anterior"
        # en cuanto insertemos este.
        carried_over_from = (
            self._resolve_carried_over_state(db_session) if self._owns_state_machine else None
        )

        # Tambien antes de crear el bot_run nuevo, para no cerrarlo a el: los
        # RUNNING que sobrevivieron a un SIGKILL se cierran aca, no se dejan
        # convivir con el run nuevo. No interfiere con el carry-over de arriba,
        # que se resuelve por bot_state y por started_at, no por status.
        #
        # Flush propio, separado del insert de mas abajo: si ese insert choca
        # contra la constraint de concurrencia y se revierte, este cleanup ya
        # esta aplicado dentro de la transaccion y no se pierde con el.
        self._close_orphan_runs(db_session)
        db_session.flush()

        bot_run = BotRun(
            environment=environment.value,
            app_version=APP_VERSION,
            config_snapshot=cfg.model_dump(mode="json"),
            status="RUNNING",
        )
        # _close_orphan_runs() de arriba no elimina la race real entre dos
        # arranques concurrentes — ver uq_bot_runs_single_running en
        # backend/storage/models.py (BotRun.__table_args__) para el porqué.
        #
        # El insert va en su propio savepoint (mismo patrón que
        # LoginLockoutRepository.get_or_create en backend/storage/repositories/
        # auth.py): si choca, solo se revierte este insert, no el flush de
        # arriba. Y el choque se re-verifica antes de traducirlo: un
        # IntegrityError con otro origen (no la carrera esperada) se relanza
        # tal cual, no se reinterpreta a ciegas como BotRunAlreadyActiveError.
        try:
            with db_session.begin_nested():
                db_session.add(bot_run)
                db_session.flush()
        except IntegrityError as exc:
            concurrent_active = BotRunRepository(db_session).get_active()
            if concurrent_active is None:
                # No fue la carrera que este except cubre: no hay otro RUNNING
                # que explique el choque, asi que no hay nada que traducir.
                raise
            # Solo cerramos la sesion si la creamos nosotros (session=None en
            # el constructor): si el caller la inyecto, sigue siendo dueño de
            # su ciclo de vida y puede seguir usandola despues de este error.
            if session is None:
                db_session.close()
            raise BotRunAlreadyActiveError(
                f"Ya existe un BotRun RUNNING ({concurrent_active.id}) — probable arranque "
                "concurrente del worker. No se crea un segundo BotRun activo."
            ) from exc
        self._bot_run = bot_run
        self._db_session = db_session

        if carried_over_from is not None:
            # Sin esto, el bot_state del bot_run nuevo queda vacio: el dashboard
            # y el endpoint del kill switch (_current_bot_state) lo leerian como
            # ACTIVE por defecto, en desacuerdo con la state machine en memoria
            # del worker que ya arranco en el estado arrastrado. self._state_machine.state
            # ya quedo seteado por force_set() dentro de _resolve_carried_over_state.
            BotStateRepository(db_session).save(
                BotStateRow(
                    bot_run_id=bot_run.id,
                    state=self._state_machine.state.value,
                    previous_state=BotState.ACTIVE.value,
                    reason=(
                        f"Arrastrado del bot_run anterior {carried_over_from.id} "
                        "al reiniciar el worker: el estado detenido requiere "
                        "revision manual antes de retomar."
                    ),
                )
            )

        # Un solo commit: el bot_run nuevo y su bot_state arrastrado (si aplica)
        # nacen atomicamente, o ninguno de los dos.
        db_session.commit()

        return adapter, db_session, bot_run, cfg

    @staticmethod
    def _close_orphan_runs(db_session: Session) -> None:
        """Marca CRASHED los BotRun que quedaron en RUNNING de arranques anteriores.

        Sin esto, cada worker que muere por SIGKILL deja una fila RUNNING que no
        se cierra nunca, y el kill switch de F15 puede terminar escribiendo el
        bot_state de una corrida que ya no existe mientras el worker vivo
        sincroniza contra la suya (ver el comentario de get_active()).

        Se loguea cada huerfano: dos corridas en RUNNING significan que la
        anterior no hizo shutdown limpio, y eso amerita mirar por que murio.
        """
        orphans = BotRunRepository(db_session).close_orphan_running(
            reason=(
                f"Cerrado como CRASHED por el arranque del worker {APP_VERSION}: "
                "quedo en RUNNING sin shutdown limpio (probable SIGKILL)."
            )
        )
        for orphan in orphans:
            log.warning(
                "orchestrator.orphan_bot_run_closed",
                orphan_bot_run_id=orphan.id,
                orphan_started_at=orphan.started_at.isoformat(),
            )

    def _resolve_carried_over_state(self, db_session: Session) -> BotRun | None:
        """Si el ultimo BotRun no quedo corriendo (kill switch o HALTED), este arranca igual.

        bot_state esta scopeado por bot_run_id, y cada arranque del worker crea
        un bot_run nuevo (ver _prepare_paper_context) con la state machine en
        ACTIVE por defecto. Sin esto, un restart del worker (deploy, crash,
        `docker compose restart`) reactiva el bot silenciosamente aunque el
        run anterior haya quedado frenado explicitamente — el kill switch exige
        revision manual antes de retomar (PDF 4.8), no un simple reinicio de
        proceso, y lo mismo aplica a cualquier otro estado que detenga el ciclo.

        Se pregunta a la state machine (is_running()) en vez de comparar contra
        KILL_SWITCH_TRIGGERED puntualmente: hoy el unico writer de bot_state es
        el endpoint del kill switch, pero el dia que el risk engine escriba
        HALTED este chequeo ya lo cubre sin tener que volver a tocarlo.

        Devuelve el BotRun del que se arrastro el estado, o None si no aplica
        (no hay corridas previas, el estado persistido es invalido, o la
        ultima corrida seguia corriendo).
        """
        previous_run = BotRunRepository(db_session).get_most_recent()
        if previous_run is None:
            return None
        previous_state = BotStateRepository(db_session).get_latest(previous_run.id)
        if previous_state is None:
            return None
        try:
            persisted_state = BotState(previous_state.state)
        except ValueError:
            return None
        if BotStateMachine(initial=persisted_state).is_running():
            return None
        self._state_machine.force_set(
            persisted_state,
            reason=f"carried_over_from_bot_run:{previous_run.id}",
        )
        log.warning(
            "orchestrator.kill_switch_carried_over",
            previous_bot_run_id=previous_run.id,
            previous_reason=previous_state.reason,
            carried_over_state=persisted_state.value,
        )
        return previous_run

    @staticmethod
    def _build_market_data_service(
        adapter: PaperAdapter, db_session: Session, bot_run: BotRun, cfg: AppConfig
    ) -> MarketDataCycleService:
        fetcher = MockDataFetcher()
        engine = MarketDataEngine(db_session, bot_run.id)
        analysis_service = MarketAnalysisService(db_session, bot_run.id)
        return MarketDataCycleService(
            adapter=adapter,
            fetcher=fetcher,
            engine=engine,
            session=db_session,
            symbols=cfg.trading.allowed_symbols,
            on_snapshot=analysis_service.on_snapshot,
        )

    def _build_execution_engine(
        self, adapter: PaperAdapter, db_session: Session, bot_run: BotRun, cfg: AppConfig
    ) -> ExecutionEngine:
        position_manager = PositionManager(adapter)
        self._position_manager = position_manager
        execution_engine = ExecutionEngine(
            adapter=adapter,
            position_manager=position_manager,
            session=db_session,
            bot_run_id=bot_run.id,
            environment=cfg.execution.environment,
            position_management_defaults=cfg.position_management,
            place_order_timeout_seconds=cfg.execution.place_order_timeout_seconds,
        )
        self._execution_engine = execution_engine
        return execution_engine

    @staticmethod
    def _build_position_tick_service(
        mds: MarketDataCycleService, position_manager: PositionManager
    ) -> PositionTickService:
        """Arma el PositionTickService (F14) con el PositionManager compartido de
        ExecutionEngine y una fuente de mark_price real.

        Fuente de mark_price: `mds.get_last_price`, el cache en memoria que
        MarketDataCycleService puebla en cada tick exitoso. CycleRunner tickea
        market_data_service antes que position_tick_service (ver cycle_runner.py),
        así que el precio ya está disponible del mismo ciclo.

        Contrato de timeout (comentario #4 del review de Rodrigo en el PR #95):
        get_mark_price no impone timeout propio acá porque no hace I/O — es una
        lectura de dict en memoria ya poblado, no puede colgarse. El timeout real
        queda pendiente para cuando exista un feed de mark_price sobre un exchange
        real (BingXAdapter, F16/F17), que sí es una llamada bloqueante.

        Si todavía no hay precio cacheado para el símbolo (nunca corrió un tick
        de market data exitoso), get_mark_price lanza LookupError — PositionTickService.
        tick_all() ya aísla esa falla por símbolo y reintenta en el próximo ciclo.
        """

        def get_mark_price(symbol: str) -> Decimal:
            price = mds.get_last_price(symbol)
            if price is None:
                raise LookupError(
                    f"No hay last_price cacheado para {symbol!r} todavia — "
                    "ningun tick de market data exitoso corrio aun para este simbolo."
                )
            return price

        return PositionTickService(position_manager, get_mark_price=get_mark_price)

    def _build_reconciliation_gate(
        self,
        adapter: PaperAdapter,
        position_manager: PositionManager,
        db_session: Session,
        bot_run: BotRun,
        cfg: AppConfig,
    ) -> ReconciliationGate:
        """Arma el ReconciliationGate (F16 [159]) con el adapter/PositionManager
        compartidos de ExecutionEngine — mismo criterio que _build_position_tick_service:
        reusar el estado real en vez de instanciar uno divergente. Unifica lo que
        antes hacia por separado OrphanOrderScanner (F16 [115], retirado): su
        deteccion (ordenes vivas sin fila local, posiciones sin PositionConfig)
        es un subconjunto estricto de la que ya hace ReconciliationEngine —
        correr ambos duplicaba las llamadas al exchange en cada tick sin
        agregar cobertura.

        PositionRepository/OrderRepository no se instancian en ningun otro lugar
        de produccion hoy (ExecutionEngine arma su propio OrderRepository interno,
        pero no expone uno compartido) — se crean acá, sobre la misma db_session
        del resto del pipeline.
        """
        engine = ReconciliationEngine(
            adapter=adapter,
            position_repo=PositionRepository(db_session),
            order_repo=OrderRepository(db_session),
            position_manager=position_manager,
            symbols=frozenset(cfg.trading.allowed_symbols),
        )
        return ReconciliationGate(
            engine=engine,
            config=cfg.reconciliation,
            state_machine=self._state_machine,
            session=db_session,
            bot_run_id=bot_run.id,
        )

    def _build_connection_health_monitor(
        self, db_session: Session, bot_run: BotRun, cfg: AppConfig
    ) -> ConnectionHealthMonitor:
        """Arma el ConnectionHealthMonitor (F16 [117]) con la misma state_machine/
        sesion/bot_run que el resto del pipeline — mismo criterio que
        _build_reconciliation_gate: reusar el estado real, no instanciar uno
        divergente.
        """
        return ConnectionHealthMonitor(
            state_machine=self._state_machine,
            session=db_session,
            bot_run_id=bot_run.id,
            max_clock_skew_ms=cfg.connection_health.max_clock_skew_ms,
            max_latency_ms=cfg.connection_health.max_latency_ms,
            symbols=frozenset(cfg.trading.allowed_symbols),
        )

    @property
    def state_machine(self) -> BotStateMachine:
        return self._state_machine

    @property
    def cycle_runner(self) -> CycleRunner:
        return self._cycle_runner

    @property
    def position_manager(self) -> PositionManager | None:
        """PositionManager compartido, para que [143] (PositionTickService) lo reuse
        en vez de instanciar uno nuevo con estado divergente. `None` si nunca se
        armo el pipeline real (cycle_runner inyectado)."""
        return self._position_manager

    @property
    def execution_engine(self) -> ExecutionEngine | None:
        return self._execution_engine

    def install_signal_handlers(self) -> None:
        """Conecta SIGTERM/SIGINT a request_shutdown del cycle runner.

        Separado del constructor para que los tests no toquen signals
        globales del proceso de pytest.
        """
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._signals_installed = True
        log.info("orchestrator.signals_installed")

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("orchestrator.signal_received", signal=signum)
        self._cycle_runner.request_shutdown()

    def run(self) -> None:
        """Arranca el loop. Bloquea hasta shutdown."""
        environment = get_config().execution.environment.value
        log.info(
            "orchestrator.start",
            environment=environment,
            initial_state=self._state_machine.state.value,
        )
        try:
            self._cycle_runner.run()
        finally:
            # finally: garantiza el cierre del BotRun incluso si el loop
            # levanta una excepcion no controlada, no solo en shutdown limpio.
            self.close()
        log.info("orchestrator.stopped", final_state=self._state_machine.state.value)

    def close(self) -> None:
        """Cierra (status STOPPED) el BotRun armado por este Orchestrator y su sesion propia.

        Tambien libera el thread pool interno del ExecutionEngine propio, si
        hay uno. Idempotente. No hace nada si bot_run/session/execution_engine
        fueron inyectados externamente (el caller es dueño de ese ciclo de
        vida), si nunca se armo el pipeline real, o si ya se cerro antes.

        run() la invoca automaticamente en su finally. Publico para que
        callers que instancian Orchestrator() sin llegar a invocar run()
        (scripts, tooling) puedan liberar la sesion y cerrar el BotRun
        explicitamente.
        """
        if self._execution_engine is not None:
            self._execution_engine.close()
            self._execution_engine = None
        self._position_manager = None
        if self._bot_run is None or self._db_session is None:
            return
        BotRunRepository(self._db_session).close(self._bot_run)
        self._db_session.commit()
        self._db_session.close()
        self._bot_run = None
        self._db_session = None
