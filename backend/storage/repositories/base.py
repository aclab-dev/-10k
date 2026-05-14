"""Repositorio base genérico para todos los agregados."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.database import Base


class BaseRepository[T: Base]:
    """CRUD genérico sobre un modelo SQLAlchemy.

    Convención de transaccionalidad: save/delete hacen flush (visibles dentro de la
    misma sesión) pero NO commitean. El caller decide cuándo commitear o hacer rollback.
    Esto permite agrupar múltiples operaciones de distintos repos en una sola transacción.
    """

    model: type[T]

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def save(self, instance: T) -> T:
        """Persiste (add + flush) y devuelve la instancia con ID asignado."""
        self._session.add(instance)
        self._session.flush()
        return instance

    def save_all(self, instances: list[T]) -> list[T]:
        for obj in instances:
            self._session.add(obj)
        self._session.flush()
        return instances

    def delete(self, instance: T) -> None:
        self._session.delete(instance)
        self._session.flush()

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def get_by_id(self, id: str) -> T | None:
        return self._session.get(self.model, id)

    def list_by_bot_run(
        self, bot_run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[T]:
        """Lista registros filtrados por bot_run_id.

        Requiere que el modelo tenga columna `bot_run_id`. Los repos cuyo
        modelo no tiene esa columna directa (PositionEventRepository,
        ModelResponseRepository, etc.) deben sobrescribir este método con
        el JOIN correspondiente.
        """
        stmt = (
            select(self.model)
            .where(self.model.bot_run_id == bot_run_id)  # type: ignore[attr-defined]
            .order_by(self.model.id)  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def _where(self, **filters: Any) -> list[T]:
        """Filtro genérico por columnas exactas."""
        stmt = select(self.model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(self.model, col) == val)
        return list(self._session.scalars(stmt))

    # ------------------------------------------------------------------
    # Control de transacción
    # ------------------------------------------------------------------

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()
