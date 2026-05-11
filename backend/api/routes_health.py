"""Endpoint /health — estado, versión y modo de la aplicación."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.core.config import Settings, get_settings

router = APIRouter()


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    return {
        "status": "ok",
        "version": settings.app_version,
        "mode": settings.environment.value,
    }
