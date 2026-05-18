"""Cycle runner: ejecuta el loop principal del bot.

Cada iteracion del ciclo:
  1) consulta el state machine para saber si debe seguir corriendo
  2) registra heartbeat (archivo + log estructurado)
  3) duerme el intervalo configurado, respondiendo rapido a shutdown

La logica real del ciclo (Market Data -> Quant -> Risk -> Execution)
se agrega en fases posteriores; este skeleton solo asegura que el
proceso este vivo y respete el state machine.
"""

from __future__ import annotations

import threading
from pathlib import Path

import structlog

from backend.trading_core.bot_state_machine import BotStateMachine

log = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_FILE = Path("/tmp/worker_alive")
DEFAULT_INTERVAL_SECONDS = 10


class CycleRunner:
    """Ejecuta el loop principal hasta que se senale shutdown.

    El shutdown se hace via `request_shutdown()` (thread-safe). El loop
    chequea la senal en intervalos cortos para terminar rapido ante SIGTERM.
    """

    def __init__(
        self,
        state_machine: BotStateMachine,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
        self._state_machine = state_machine
        self._interval_seconds = interval_seconds
        self._heartbeat_file = heartbeat_file
        self._shutdown_event = threading.Event()
        log.info(
            "cycle_runner.init",
            interval_seconds=interval_seconds,
            heartbeat_file=str(heartbeat_file),
        )

    def request_shutdown(self) -> None:
        """Senala al loop que debe terminar. Idempotente."""
        if not self._shutdown_event.is_set():
            log.info("cycle_runner.shutdown_requested")
        self._shutdown_event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def heartbeat_file(self) -> Path:
        return self._heartbeat_file

    def run(self) -> None:
        """Loop principal. Bloquea hasta que se pida shutdown."""
        log.info(
            "cycle_runner.start",
            state=self._state_machine.state.value,
            interval_seconds=self._interval_seconds,
        )
        while not self._shutdown_event.is_set():
            if not self._state_machine.is_running():
                log.info("cycle_runner.paused_by_state", state=self._state_machine.state.value)
            else:
                self._tick()
            # Espera segmentada para responder rapido a shutdown.
            if self._shutdown_event.wait(timeout=self._interval_seconds):
                break
        log.info("cycle_runner.stopped")

    def _tick(self) -> None:
        """Una iteracion del ciclo. Por ahora solo heartbeat."""
        self._heartbeat_file.touch(exist_ok=True)
        log.info("cycle_runner.heartbeat", state=self._state_machine.state.value)


def parse_interval_from_env(raw: str | None, default: int = DEFAULT_INTERVAL_SECONDS) -> int:
    """Convierte la variable de entorno a int con validacion.

    Lanza ValueError con mensaje claro si el valor no es un int positivo,
    para que el orchestrator falle rapido al arrancar en vez de en runtime.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"WORKER_HEARTBEAT_INTERVAL_SECONDS must be an int, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"WORKER_HEARTBEAT_INTERVAL_SECONDS must be > 0, got {value}")
    return value


__all__ = [
    "DEFAULT_HEARTBEAT_FILE",
    "DEFAULT_INTERVAL_SECONDS",
    "CycleRunner",
    "parse_interval_from_env",
]
