"""Test de integración: 'a lo sumo un BotRun RUNNING' bajo concurrencia real (F16 [114]).

Antes de esta tarjeta, ese invariante solo vivía en la aplicación
(BotRunRepository.get_active() / Orchestrator._close_orphan_runs()) — nada en la
DB impedía que dos arranques concurrentes del worker (ej. ventana de un rolling
restart) insertaran dos filas RUNNING a la vez. El índice único parcial
uq_bot_runs_single_running (migración d92a4c17e8f3) lo convierte en una garantía
real de Postgres, y esto se puede verificar con SQLite in-memory (no soporta
índices parciales) — hace falta Postgres real y transacciones concurrentes de
verdad, igual razón que test_login_throttle_concurrency.py.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.storage.models import BotRun
from backend.storage.repositories.bot import BotRunRepository

pytestmark = pytest.mark.integration

_CONCURRENT_STARTS = 8


@pytest.fixture
def limpio(pg_session: Session) -> None:
    """bot_runs es global (`_try_start_bot_run` commitea, `pg_session` no lo revierte).

    Limpia antes por si otro test dejó filas RUNNING sueltas, y después porque
    este test siempre deja una — sin esto, el próximo test que necesite crear
    un BotRun RUNNING (en cualquier archivo, misma sesión de pytest) choca con
    uq_bot_runs_single_running contra una fila que ya no tiene nada que ver.
    """
    pg_session.execute(delete(BotRun))
    pg_session.commit()
    yield
    pg_session.execute(delete(BotRun))
    pg_session.commit()


def _try_start_bot_run(session_factory: sessionmaker, index: int) -> bool:
    """Simula un arranque de worker: intenta insertar un BotRun RUNNING.

    True si el insert tuvo éxito, False si el índice único parcial lo rechazó.
    Cada intento usa su propia sesión/transacción, como dos procesos reales.
    """
    with session_factory() as session:
        session.add(
            BotRun(
                started_at=datetime.now(UTC),
                environment="PAPER",
                app_version=f"test-{index}",
                config_snapshot={},
                status="RUNNING",
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
        return True


def test_concurrent_starts_only_one_bot_run_ends_up_running(
    pg_session_factory: sessionmaker, pg_session: Session, limpio: None
) -> None:
    """N arranques concurrentes → exactamente 1 insert exitoso, el resto rechazado
    por uq_bot_runs_single_running, sin duplicar la fila RUNNING."""
    with ThreadPoolExecutor(max_workers=_CONCURRENT_STARTS) as pool:
        futures = [
            pool.submit(_try_start_bot_run, pg_session_factory, i)
            for i in range(_CONCURRENT_STARTS)
        ]
        results = [f.result() for f in futures]

    assert sum(results) == 1, "Exactamente un arranque concurrente debe ganar la carrera"

    repo = BotRunRepository(pg_session)
    running = [r for r in pg_session.query(BotRun).all() if r.status == "RUNNING"]
    assert len(running) == 1
    assert repo.get_active() is not None
    assert repo.get_active().id == running[0].id
