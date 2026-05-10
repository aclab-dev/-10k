"""Worker placeholder para que docker compose levante el servicio worker.

Solo hace heartbeat: actualiza /tmp/worker_alive (lo lee el healthcheck)
y loguea a stdout. La logica real (Trading Core, ciclo, kill switch)
se incorpora en tarjetas posteriores.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType

HEARTBEAT_FILE = Path("/tmp/worker_alive")
DEFAULT_INTERVAL_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    log.info("received signal %s, shutting down", signum)
    _shutdown = True


def main() -> None:
    interval = int(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))
    environment = os.getenv("ENVIRONMENT", "PAPER")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("worker started (environment=%s, heartbeat_interval=%ss)", environment, interval)

    while not _shutdown:
        HEARTBEAT_FILE.touch(exist_ok=True)
        log.info("heartbeat")
        # Sleep en pasos cortos para responder rapido a SIGTERM.
        for _ in range(interval):
            if _shutdown:
                break
            time.sleep(1)

    log.info("worker stopped")


if __name__ == "__main__":
    main()
