"""Fixtures compartidos para tests de integración.

Levanta un contenedor PostgreSQL real usando testcontainers.
El contenedor vive durante toda la sesión de pytest para minimizar
el overhead de arranque.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
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


@pytest.fixture
def clean_pg_session(pg_engine) -> Session:
    """Sesión que trunca todas las tablas del Anexo B después del test.

    Usar cuando el rollback no es suficiente (ej. tests que hacen commit
    explícito para verificar constraints diferidos).
    """
    with Session(pg_engine) as session:
        yield session
        # Truncar en orden inverso a las FK para evitar violaciones
        session.execute(text(
            "TRUNCATE TABLE "
            "historical_replay_snapshots, historical_replay_runs, "
            "backtest_results, backtest_runs, "
            "token_usage, news_context, kill_switch_events, "
            "system_events, errors, "
            "position_events, positions, orders, trades, "
            "risk_validations, decision_aggregations, decisions, "
            "model_responses, model_requests, "
            "feature_packages, volatility_assessments, "
            "market_regimes, quant_signals, market_snapshots, "
            "accounts_state, bot_state, bot_runs "
            "CASCADE"
        ))
        session.commit()
