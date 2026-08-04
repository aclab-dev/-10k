"""FastAPI skeleton — expone /health con versión y modo de ejecución."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from backend.api.routes_decisions import router as decisions_router
from backend.api.routes_health import router as health_router
from backend.api.routes_risk import router as risk_router
from backend.api.routes_status import router as status_router
from backend.api.routes_tokens import router as tokens_router
from backend.core.config import APP_VERSION, get_settings
from backend.core.logging import configure_logging

configure_logging()

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
app.include_router(status_router, prefix="/api")
app.include_router(decisions_router, prefix="/api")
app.include_router(risk_router, prefix="/api")
app.include_router(tokens_router, prefix="/api")
