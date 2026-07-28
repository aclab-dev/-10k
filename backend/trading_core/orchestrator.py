"""Orchestrator: arma el grafo (state machine + cycle runner) y lo corre.

Es el punto de entrada logico del worker. Lee configuracion del entorno,
instancia los componentes en el orden correcto (incluyendo el ExchangeAdapter
y el Market Data Engine reales para PAPER) y bloquea hasta que el loop
termine (shutdown via signal o estado terminal).
"""

from __future__ import annotations

import os
import signal
from decimal import Decimal
from types import FrameType

import structlog
from sqlalchemy.orm import Session

from backend.core.config import APP_VERSION, Environment, get_config
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import MockDataFetcher
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
    pipeline real de market data (PaperAdapter + MockDataFetcher en PAPER).
    """

    def __init__(
        self,
        state_machine: BotStateMachine | None = None,
        cycle_runner: CycleRunner | None = None,
        market_data_service: MarketDataCycleService | None = None,
        session: Session | None = None,
    ) -> None:
        self._state_machine = state_machine or BotStateMachine(initial=BotState.ACTIVE)
        # Solo se pueblan si este Orchestrator arma su propio BotRun (ver
        # _build_market_data_service). Si cycle_runner/market_data_service vienen
        # inyectados, el caller es dueño de ese ciclo de vida, no nosotros.
        self._bot_run: BotRun | None = None
        self._db_session: Session | None = None
        if cycle_runner is None:
            interval = parse_interval_from_env(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS"))
            mds = market_data_service or self._build_market_data_service(session)
            self._cycle_runner = CycleRunner(
                self._state_machine, interval_seconds=interval, market_data_service=mds
            )
        else:
            self._cycle_runner = cycle_runner
        self._signals_installed = False

    def _build_market_data_service(self, session: Session | None) -> MarketDataCycleService:
        """Arma el pipeline real de market data segun el environment configurado.

        Solo PAPER esta soportado: BingX real (TESTNET/LIVE) es F16/F17 y sigue
        bloqueado, asi que correr en otro environment falla rapido en vez de
        operar silenciosamente con PaperAdapter fuera de PAPER.

        Guarda bot_run/session en self para que run() pueda cerrar el BotRun
        (status STOPPED) al hacer shutdown graceful, evitando dejarlo colgado
        en RUNNING y rompiendo la semantica de BotRunRepository.get_active().
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
        fetcher = MockDataFetcher()
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

        engine = MarketDataEngine(db_session, bot_run.id)
        return MarketDataCycleService(
            adapter=adapter,
            fetcher=fetcher,
            engine=engine,
            session=db_session,
            symbols=cfg.trading.allowed_symbols,
        )

    @property
    def state_machine(self) -> BotStateMachine:
        return self._state_machine

    @property
    def cycle_runner(self) -> CycleRunner:
        return self._cycle_runner

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
        self._cycle_runner.run()
        self._close_bot_run()
        log.info("orchestrator.stopped", final_state=self._state_machine.state.value)

    def _close_bot_run(self) -> None:
        """Cierra (status STOPPED) el BotRun armado por este Orchestrator, si lo creo el mismo.

        No hace nada si bot_run/session fueron inyectados externamente (el
        caller es dueño de ese ciclo de vida) o si nunca se armo el pipeline
        real de market data.
        """
        if self._bot_run is None or self._db_session is None:
            return
        BotRunRepository(self._db_session).close(self._bot_run)
        self._db_session.commit()
        self._db_session.close()
