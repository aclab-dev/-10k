"""Worker entry point. Toda la logica vive en backend.trading_core.

Solo instancia el Orchestrator, instala signal handlers y arranca el loop.
"""

from __future__ import annotations

import sys

import structlog

from backend.core.logging import configure_logging
from backend.trading_core.orchestrator import BotRunAlreadyActiveError, Orchestrator

log = structlog.get_logger(__name__)


def main() -> None:
    configure_logging()

    # BotRunAlreadyActiveError (F16 [114]) no es un fallo transitorio: significa
    # que otro worker ya tiene un BotRun RUNNING, y reintentar no lo resuelve.
    # docker-compose igual va a reiniciar este proceso (restart: unless-stopped,
    # sin backoff), así que esto no corta el crash-loop -- lo único que hace es
    # dejar un log distinguible de un crash genérico para quien lo investigue.
    # Cortar el loop de verdad es una decisión de infraestructura (backoff en
    # el restart policy, o un supervisor que no reintente este caso) que queda
    # fuera de este cambio.
    try:
        orchestrator = Orchestrator()
    except BotRunAlreadyActiveError as exc:
        log.critical("worker.bot_run_already_active", error=str(exc))
        sys.exit(1)

    orchestrator.install_signal_handlers()
    orchestrator.run()


if __name__ == "__main__":
    main()
