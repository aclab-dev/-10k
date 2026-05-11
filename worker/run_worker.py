"""Worker entry point. Toda la logica vive en backend.trading_core.

Solo instancia el Orchestrator, instala signal handlers y arranca el loop.
"""

from __future__ import annotations

import structlog

from backend.core.logging import configure_logging
from backend.trading_core.orchestrator import Orchestrator


def main() -> None:
    configure_logging()
    orchestrator = Orchestrator()
    orchestrator.install_signal_handlers()
    orchestrator.run()


if __name__ == "__main__":
    main()
