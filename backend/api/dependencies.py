"""Dependencias compartidas de FastAPI para los endpoints REST del dashboard."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.storage.database import get_db
from backend.storage.models import BotRun
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


def get_current_bot_run(
    db: Annotated[Session, Depends(get_db)],
    bot_run_id: Annotated[
        str | None,
        Query(description="Bot run a consultar. Por defecto, el bot run activo (RUNNING)."),
    ] = None,
) -> BotRun:
    """Resuelve el BotRun a usar: el pasado explícitamente o el activo.

    404 si el bot_run_id pasado no existe (o no tiene formato UUID), o si no
    hay ningún bot run activo cuando no se pasó ninguno. Única query de
    resolución — endpoints que necesitan el objeto completo (no solo el id)
    deben depender de esta función en vez de volver a buscarlo.
    """
    if bot_run_id is not None:
        raise_404_if_not_uuid(bot_run_id, param_name="bot_run_id")
        bot_run = BotRunRepository(db).get_by_id(bot_run_id)
        if bot_run is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"bot_run_id '{bot_run_id}' no encontrado"
            )
        return bot_run

    active = BotRunRepository(db).get_active()
    if active is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay ningún bot run activo")
    return active


def get_current_bot_run_id(
    bot_run: Annotated[BotRun, Depends(get_current_bot_run)],
) -> str:
    """Atajo para endpoints que solo necesitan el id, no el objeto completo."""
    return bot_run.id


def _as_utc(value: datetime | None) -> datetime | None:
    """Normaliza a UTC un datetime del query string.

    Un valor naive se interpreta como UTC en vez de dejarlo pasar: las columnas
    timestamp son TIMESTAMPTZ y PostgreSQL resolvería el naive contra el
    TimeZone de la sesión, así que el mismo filtro daría resultados distintos
    según cómo esté configurado el server. El frontend manda siempre offset
    explícito; esto solo cubre llamadas manuales a la API.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class TimeRange:
    """Dependencia de FastAPI para el filtro por fecha de los listados de historial.

    Rango semiabierto `[from_ts, to_ts)`, ambos opcionales y normalizados a UTC.
    """

    def __init__(
        self,
        from_ts: Annotated[
            datetime | None,
            Query(description="Filtra timestamp >= from_ts (ISO 8601). Naive se asume UTC."),
        ] = None,
        to_ts: Annotated[
            datetime | None,
            Query(description="Filtra timestamp < to_ts (ISO 8601). Naive se asume UTC."),
        ] = None,
    ) -> None:
        self.from_ts = _as_utc(from_ts)
        self.to_ts = _as_utc(to_ts)
        if self.from_ts is not None and self.to_ts is not None and self.from_ts > self.to_ts:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "rango inválido: from_ts es posterior a to_ts",
            )
