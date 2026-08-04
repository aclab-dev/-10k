"""Paginación compartida (limit/offset) para los endpoints REST del dashboard."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class PageParams:
    """Dependencia de FastAPI que valida y agrupa `limit`/`offset`."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


class Page[T](BaseModel):
    """Respuesta paginada genérica: items de la página actual + total real filtrado."""

    items: list[T]
    total: int
    limit: int
    offset: int
