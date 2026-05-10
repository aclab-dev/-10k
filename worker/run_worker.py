"""Worker entry point. Toda la logica vive en backend.trading_core.

Solo instancia el Orchestrator, instala signal handlers y arranca el loop.
"""

from __future__ import annotations

import logging
import sys

import structlog

from backend.trading_core.orchestrator import Orchestrator


def _configure_logging() -> None:
    """Configuracion minima de structlog: JSON friendly via stdlib root."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    _configure_logging()
    orchestrator = Orchestrator()
    orchestrator.install_signal_handlers()
    orchestrator.run()


if __name__ == "__main__":
    main()
