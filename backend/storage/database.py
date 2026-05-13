"""Engine, sesión y Base declarativa de SQLAlchemy."""

from __future__ import annotations

import threading
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy import Engine


class PgJSON(TypeDecorator):  # type: ignore[type-arg]
    """JSONB en PostgreSQL, JSON en cualquier otro dialecto (ej. SQLite en tests)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_engine_lock = threading.Lock()
_SessionLocal: sessionmaker | None = None  # type: ignore[type-arg]


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from backend.core.config import get_settings

                settings = get_settings()
                _engine = create_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    pool_size=settings.db_pool_size,
                    max_overflow=settings.db_max_overflow,
                )
    return _engine


def get_session_factory() -> sessionmaker:  # type: ignore[type-arg]
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
