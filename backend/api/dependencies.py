"""Dependencias compartidas de FastAPI para los endpoints REST del dashboard."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.storage.database import get_db
from backend.storage.repositories import BotRunRepository


def get_current_bot_run_id(
    db: Annotated[Session, Depends(get_db)],
    bot_run_id: Annotated[
        str | None,
        Query(description="Bot run a consultar. Por defecto, el bot run activo (RUNNING)."),
    ] = None,
) -> str:
    """Resuelve el bot_run_id a usar en la query: el pasado explícitamente o el activo.

    404 si el bot_run_id pasado no existe, o si no hay ningún bot run activo
    cuando no se pasó ninguno.
    """
    repo = BotRunRepository(db)
    if bot_run_id is not None:
        bot_run = repo.get_by_id(bot_run_id)
        if bot_run is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"bot_run_id '{bot_run_id}' no encontrado"
            )
        return bot_run_id

    active = repo.get_active()
    if active is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay ningún bot run activo")
    return active.id
