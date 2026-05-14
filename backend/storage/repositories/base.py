from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.storage.database import Base


class BaseRepository[T: Base]:
    _model: type[T]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "_model" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must define '_model'")

    def create(self, session: Session, **kwargs: Any) -> T:
        obj = self._model(**kwargs)
        session.add(obj)
        session.flush()
        return obj
