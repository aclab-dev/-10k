"""Orchestrator: arma el grafo (state machine + cycle runner) y lo corre.

Es el punto de entrada logico del worker. Lee configuracion del entorno,
instancia los componentes en el orden correcto y bloquea hasta que el
loop termine (shutdown via signal o estado terminal).
"""

from __future__ import annotations

import os
import signal
from types import FrameType

import structlog

from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner, parse_interval_from_env

log = structlog.get_logger(__name__)


class Orchestrator:
    """Coordina state machine + cycle runner.

    Se puede construir con dependencias inyectadas (para tests) o sin
    argumentos, en cuyo caso lee de variables de entorno.
    """

    def __init__(
        self,
        state_machine: BotStateMachine | None = None,
        cycle_runner: CycleRunner | None = None,
    ) -> None:
        self._state_machine = state_machine or BotStateMachine(initial=BotState.ACTIVE)
        if cycle_runner is None:
            interval = parse_interval_from_env(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS"))
            self._cycle_runner = CycleRunner(self._state_machine, interval_seconds=interval)
        else:
            self._cycle_runner = cycle_runner
        self._signals_installed = False

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
        environment = os.getenv("ENVIRONMENT", "PAPER")
        log.info(
            "orchestrator.start",
            environment=environment,
            initial_state=self._state_machine.state.value,
        )
        self._cycle_runner.run()
        log.info("orchestrator.stopped", final_state=self._state_machine.state.value)
