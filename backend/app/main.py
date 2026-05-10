"""FastAPI skeleton — expone /health con versión y modo de ejecución."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import structlog.stdlib
from fastapi import FastAPI

from backend.api.routes_health import router as health_router
from backend.core.config import APP_VERSION, get_settings

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)

_log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _log.info("app_started", version=APP_VERSION, mode=settings.environment.value)
    yield


app = FastAPI(
    title="crypto-futures-gpt55-bot",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.include_router(health_router)
