"""Fixtures compartidos para tests de integración.

Levanta un contenedor PostgreSQL real usando testcontainers.
El contenedor vive durante toda la sesión de pytest para minimizar
el overhead de arranque.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from backend.storage.database import Base


@pytest.fixture(scope="session")
def pg_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_engine(pg_container: PostgresContainer):
    url = pg_container.get_connection_url()
    engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="session")
def pg_session_factory(pg_engine):
    return sessionmaker(pg_engine, autocommit=False, autoflush=False)


@pytest.fixture
def pg_session(pg_session_factory) -> Session:
    """Sesión limpia por test: hace rollback al finalizar."""
    with pg_session_factory() as session:
        yield session
        session.rollback()
