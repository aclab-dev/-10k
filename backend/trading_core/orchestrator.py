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
from sqlalchemy.orm import Session

from backend.core.config import APP_VERSION, AppConfig, Environment, get_config
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.execution.engine import ExecutionEngine
from backend.market_data.analysis_service import MarketAnalysisService
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import MockDataFetcher
from backend.position_manager.manager import PositionManager
from backend.storage.database import get_session_factory
from backend.storage.models import BotRun
from backend.storage.repositories.bot import BotRunRepository
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner, parse_interval_from_env

log = structlog.get_logger(__name__)


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
            else:
                # execution_engine no puede ser None acá: si lo fuera, el check
                # XOR de arriba ya habría lanzado (market_data_service no es None
                # en esta rama). Narrowing explícito para mypy.
                assert execution_engine is not None
                mds = market_data_service
                exec_engine = execution_engine
            self._cycle_runner = CycleRunner(
                self._state_machine,
                interval_seconds=interval,
                market_data_service=mds,
                execution_engine=exec_engine,
            )
        else:
            self._cycle_runner = cycle_runner
        self._signals_installed = False

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

        bot_run = BotRun(
            environment=environment.value,
            app_version=APP_VERSION,
            config_snapshot=cfg.model_dump(mode="json"),
            status="RUNNING",
        )
        db_session.add(bot_run)
        db_session.commit()
        self._bot_run = bot_run
        self._db_session = db_session

        return adapter, db_session, bot_run, cfg

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
