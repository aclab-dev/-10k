"""Worker entry point. Toda la logica vive en backend.trading_core.

Solo instancia el Orchestrator, instala signal handlers y arranca el loop.
"""

from __future__ import annotations

import os
import sys
import time

import structlog

from backend.core.logging import configure_logging
from backend.trading_core.orchestrator import BotRunAlreadyActiveError, Orchestrator

log = structlog.get_logger(__name__)

DEFAULT_STARTUP_RACE_BACKOFF_SECONDS = 300


def parse_backoff_seconds_from_env(raw: str | None) -> int:
    """Convierte WORKER_STARTUP_RACE_BACKOFF_SECONDS a int con validacion.

    Mismo criterio que parse_interval_from_env en cycle_runner.py: falla rapido
    con un mensaje claro al arrancar en vez de recien en runtime, aunque este
    valor solo se use en el camino de error de mas abajo -- una race de
    arranque puede tardar en darse, y para entonces ya es tarde para notar que
    la config estaba mal.
    """
    if raw is None or raw == "":
        return DEFAULT_STARTUP_RACE_BACKOFF_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"WORKER_STARTUP_RACE_BACKOFF_SECONDS must be an int, got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError(f"WORKER_STARTUP_RACE_BACKOFF_SECONDS must be >= 0, got {value}")
    return value


def main() -> None:
    configure_logging()
    backoff_seconds = parse_backoff_seconds_from_env(
        os.getenv("WORKER_STARTUP_RACE_BACKOFF_SECONDS")
    )

    # BotRunAlreadyActiveError (F16 [114]) no es un fallo transitorio: significa
    # que otro worker ya tiene un BotRun RUNNING, y reintentar no lo resuelve --
    # el que pierde la carrera tiene que quedarse afuera hasta que un humano lo
    # revise, no reintentar hasta que el otro eventualmente pare.
    #
    # docker-compose (restart: unless-stopped) va a reiniciar este proceso de
    # todas formas: no hay forma declarativa de que ese restart policy
    # distinga "esto va a volver a fallar seguro" de un crash transitorio
    # real, y cambiar la politica global perderia la recuperacion automatica
    # que si queremos para esos otros casos. La mitigacion queda de este lado:
    # dormir antes de salir espacia el ciclo de reinicios de Docker (5 minutos
    # por defecto en vez de reintentar cada pocos segundos), asi no hace falta
    # estar mirando los logs en el momento exacto para notarlo.
    try:
        orchestrator = Orchestrator()
    except BotRunAlreadyActiveError as exc:
        log.critical(
            "worker.bot_run_already_active", error=str(exc), backoff_seconds=backoff_seconds
        )
        time.sleep(backoff_seconds)
        sys.exit(1)

    orchestrator.install_signal_handlers()
    orchestrator.run()


if __name__ == "__main__":
    main()
