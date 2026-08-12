"""Tests de integración: el throttle del login bajo concurrencia real (F15).

Un brute-force es concurrente por definición. Contar los fallos y decidir si se
bloquea son dos pasos, y bajo READ COMMITTED varias transacciones pueden leer el
conteo antes de que las otras commiteen: sin serializar esa sección, el umbral
configurado se supera sin que ninguna request cree el lockout.

Esto no se puede verificar con SQLite in-memory, que es lo que usan los tests
unitarios: hace falta Postgres real y transacciones concurrentes de verdad.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from backend.auth.throttle import LoginThrottle
from backend.core.config import LoginThrottleConfig
from backend.storage.models import LoginAttempt, LoginLockout, LoginScope
from backend.storage.repositories.auth import LoginThrottleRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
USER = "usuario-atacado"
IP = "203.0.113.7"

MAX_FAILURES = 3
CONCURRENT_FAILURES = 12

CONFIG = LoginThrottleConfig(
    enabled=True,
    max_failures_per_username=MAX_FAILURES,
    # Alto a propósito: el scope IP toma el lock igual, pero no debe generar
    # lockouts que enturbien lo que se afirma sobre el scope USERNAME.
    max_failures_per_ip=10_000,
    window_seconds=600,
    lockout_seconds=60,
    lockout_backoff_factor=2.0,
    max_lockout_seconds=300,
)


@pytest.fixture
def limpio(pg_session: Session) -> None:
    """Deja las dos tablas vacías: son globales y otros tests pueden dejar filas."""
    pg_session.execute(delete(LoginAttempt))
    pg_session.execute(delete(LoginLockout))
    pg_session.commit()


def _fallar_una_vez(session_factory: sessionmaker, at: datetime) -> None:
    """Un intento fallido en su propia transacción, como una request real."""
    with session_factory() as session:
        LoginThrottle(session, CONFIG).record_failure(USER, IP, now=at)
        session.commit()


def test_un_burst_concurrente_no_supera_el_umbral_configurado(
    pg_session_factory: sessionmaker, pg_session: Session, limpio: None
) -> None:
    """Con la sección crítica serializada, el resultado es exacto y no probabilístico.

    12 fallos con umbral 3 tienen que producir 4 lockouts sobre el mismo usuario
    —el backoff los cuenta— y dejar 0 intentos sin consumir. Sin el lock, varias
    transacciones leen el mismo conteo y se pierden bloqueos.
    """
    with ThreadPoolExecutor(max_workers=CONCURRENT_FAILURES) as pool:
        futuros = [
            pool.submit(_fallar_una_vez, pg_session_factory, T0) for _ in range(CONCURRENT_FAILURES)
        ]
        for futuro in futuros:
            futuro.result()

    repo = LoginThrottleRepository(pg_session)
    lockout = repo.get_lockout(LoginScope.USERNAME, USER)

    assert lockout is not None
    assert lockout.lockout_count == CONCURRENT_FAILURES // MAX_FAILURES
    assert repo.count_failures_since(LoginScope.USERNAME, USER, T0 - timedelta(days=1)) == 0


def test_el_burst_deja_al_usuario_bloqueado(
    pg_session_factory: sessionmaker, pg_session: Session, limpio: None
) -> None:
    with ThreadPoolExecutor(max_workers=CONCURRENT_FAILURES) as pool:
        futuros = [
            pool.submit(_fallar_una_vez, pg_session_factory, T0) for _ in range(CONCURRENT_FAILURES)
        ]
        for futuro in futuros:
            futuro.result()

    decision = LoginThrottle(pg_session, CONFIG).check_lockout(USER, IP, now=T0)

    assert decision.locked is True
    assert decision.scope is LoginScope.USERNAME


def test_identidades_distintas_no_se_serializan_entre_si(
    pg_session_factory: sessionmaker, pg_session: Session, limpio: None
) -> None:
    """El lock es por identidad: dos usuarios distintos no se pisan ni se bloquean mutuamente."""

    def fallar(usuario: str) -> None:
        with pg_session_factory() as session:
            LoginThrottle(session, CONFIG).record_failure(usuario, IP, now=T0)
            session.commit()

    usuarios = [f"usuario-{i}" for i in range(6)]
    with ThreadPoolExecutor(max_workers=len(usuarios)) as pool:
        for futuro in [pool.submit(fallar, u) for u in usuarios]:
            futuro.result()

    repo = LoginThrottleRepository(pg_session)
    # Un fallo cada uno: nadie llega al umbral.
    assert all(repo.get_lockout(LoginScope.USERNAME, u) is None for u in usuarios)
    assert repo.count_failures_since(LoginScope.IP, IP, T0 - timedelta(days=1)) == len(usuarios)
