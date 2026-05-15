"""Endpoint /health — estado, versión y modo de la aplicación."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.storage.database import get_db

router = APIRouter()


def _db_reachable(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/health")
def health(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    base = {
        "version": settings.app_version,
        "mode": settings.environment.value,
    }
    if _db_reachable(db):
        return JSONResponse({"status": "ok", **base})
    return JSONResponse({"status": "degraded", "db": "unreachable", **base}, status_code=503)
