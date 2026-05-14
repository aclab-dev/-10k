from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

T = TypeVar("T")


class BaseRepository(Generic[T]):
    _model: type[T]

    def create(self, session: Session, **kwargs: Any) -> T:
        obj = self._model(**kwargs)
        session.add(obj)
        session.flush()
        return obj
