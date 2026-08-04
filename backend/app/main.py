"""FastAPI skeleton — expone /health con versión y modo de ejecución."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI

from backend.api.routes_auth import router as auth_router
from backend.api.routes_decisions import router as decisions_router
from backend.api.routes_health import router as health_router
from backend.api.routes_risk import router as risk_router
from backend.api.routes_status import router as status_router
from backend.api.routes_tokens import router as tokens_router
from backend.auth.config import get_auth_credentials
from backend.auth.dependencies import require_auth
from backend.core.config import APP_VERSION, get_settings
from backend.core.logging import configure_logging

configure_logging()

_log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    auth_enabled = get_auth_credentials().enabled
    _log.info(
        "app_started",
        version=APP_VERSION,
        mode=settings.environment.value,
        dashboard_auth_enabled=auth_enabled,
    )
    if not auth_enabled:
        _log.warning("auth.disabled", detail="Los endpoints del dashboard están sin protección")
    yield


app = FastAPI(
    title="crypto-futures-gpt55-bot",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Públicos: /health lo consume el healthcheck de docker-compose con curl sin
# credenciales, y /api/auth/login es por definición el endpoint pre-sesión.
app.include_router(health_router)
app.include_router(auth_router, prefix="/api")

# Sensibles: exponen estado de cuenta, PnL, decisiones y consumo de tokens.
# La dependencia va en el include_router y no en cada endpoint para que una
# ruta nueva en estos routers nazca protegida por default.
_protected = [Depends(require_auth)]
app.include_router(status_router, prefix="/api", dependencies=_protected)
app.include_router(decisions_router, prefix="/api", dependencies=_protected)
app.include_router(risk_router, prefix="/api", dependencies=_protected)
app.include_router(tokens_router, prefix="/api", dependencies=_protected)
