"""Fixtures para la suite de caos / fault injection (F16 [118]).

DB: SQLite en memoria, mismo patrón que `tests/unit/conftest.py` — los
componentes que persisten estado en estos tests (ConnectionHealthMonitor,
OrphanOrderScanner) ya se ejercitan sobre SQLite en su cobertura unitaria.
Las funciones `make_bot_run` / `make_bot_state` se reusan de ahí.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.storage.database import Base
from backend.storage.models import BotRun


@pytest.fixture
def engine() -> Generator[object, None, None]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine: object) -> Generator[Session, None, None]:
    with Session(engine) as s:  # type: ignore[arg-type]
        yield s


@pytest.fixture
def bot_run(session: Session) -> BotRun:
    """BotRun RUNNING listo para que monitor/scanner tomen su lock de fila."""
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run
