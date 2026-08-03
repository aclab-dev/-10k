"""Dependencias compartidas de FastAPI para los endpoints REST del dashboard."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.storage.database import get_db
from backend.storage.repositories import BotRunRepository


def raise_404_if_not_uuid(value: str, *, param_name: str) -> None:
    """Valida el formato UUID antes de consultarlo.

    Las columnas id son UUID(as_uuid=False): en SQLite (tests) un string con
    formato inválido simplemente no matchea nada y devuelve None, pero contra
    PostgreSQL real el driver intenta castear el literal en el servidor y
    lanza DataError (500 sin manejar). Cortamos acá con un 404 limpio.
    """
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"{param_name} '{value}' no encontrado"
        ) from exc


def get_current_bot_run_id(
    db: Annotated[Session, Depends(get_db)],
    bot_run_id: Annotated[
        str | None,
        Query(description="Bot run a consultar. Por defecto, el bot run activo (RUNNING)."),
    ] = None,
) -> str:
    """Resuelve el bot_run_id a usar en la query: el pasado explícitamente o el activo.

    404 si el bot_run_id pasado no existe (o no tiene formato UUID), o si no
    hay ningún bot run activo cuando no se pasó ninguno.
    """
    if bot_run_id is not None:
        raise_404_if_not_uuid(bot_run_id, param_name="bot_run_id")
        bot_run = BotRunRepository(db).get_by_id(bot_run_id)
        if bot_run is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"bot_run_id '{bot_run_id}' no encontrado"
            )
        return bot_run_id

    active = BotRunRepository(db).get_active()
    if active is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay ningún bot run activo")
    return active.id
