"""Repositorio del throttling de login (F15): conteo de fallos y estado de lockout.

Un solo repositorio para las dos tablas porque siempre se usan juntas: el conteo
de `login_attempts` es lo que decide si hay que escribir un `login_lockouts`, y
un login exitoso limpia ambas. Separarlos obligaría al caller a coordinar dos
objetos para cada operación.

Como el resto de los repos, no commitea: eso lo decide el caller.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select

from backend.storage.models import LoginAttempt, LoginLockout, LoginScope
from backend.storage.repositories.base import BaseRepository


class LoginThrottleRepository(BaseRepository[LoginAttempt]):
    model = LoginAttempt

    # ------------------------------------------------------------------
    # Intentos fallidos
    # ------------------------------------------------------------------

    def record_failure(self, scope: LoginScope, identifier: str, *, now: datetime) -> LoginAttempt:
        """Persiste un intento fallido para el par (scope, identifier)."""
        attempt = LoginAttempt(scope=scope.value, identifier=identifier, timestamp=now)
        return self.save(attempt)

    def count_failures_since(self, scope: LoginScope, identifier: str, since: datetime) -> int:
        """Cuenta los fallos con timestamp >= `since` (borde inferior de la ventana)."""
        stmt = (
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.scope == scope.value,
                LoginAttempt.identifier == identifier,
                LoginAttempt.timestamp >= since,
            )
        )
        return self._session.scalar(stmt) or 0

    def clear_attempts(self, scope: LoginScope, identifier: str) -> None:
        """Borra los fallos acumulados del par (scope, identifier)."""
        self._session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.scope == scope.value,
                LoginAttempt.identifier == identifier,
            )
        )
        self._session.flush()

    def purge_attempts_before(self, cutoff: datetime) -> None:
        """Descarta fallos ya fuera de cualquier ventana.

        Se llama de forma oportunista al registrar un fallo: sin esto la tabla
        crece sin techo, porque los identificadores que nunca vuelven a intentar
        no tienen quién los limpie.
        """
        self._session.execute(delete(LoginAttempt).where(LoginAttempt.timestamp < cutoff))
        self._session.flush()

    # ------------------------------------------------------------------
    # Lockouts
    # ------------------------------------------------------------------

    def get_lockout(self, scope: LoginScope, identifier: str) -> LoginLockout | None:
        """Devuelve el lockout del par, esté vigente o vencido."""
        stmt = select(LoginLockout).where(
            LoginLockout.scope == scope.value,
            LoginLockout.identifier == identifier,
        )
        return self._session.scalars(stmt).one_or_none()

    def upsert_lockout(
        self,
        scope: LoginScope,
        identifier: str,
        *,
        locked_until: datetime,
        now: datetime,
    ) -> LoginLockout:
        """Crea el lockout o lo renueva incrementando `lockout_count`.

        El contador incrementa aunque el lockout anterior ya haya vencido: la
        reincidencia es exactamente lo que el backoff tiene que castigar.
        """
        lockout = self.get_lockout(scope, identifier)
        if lockout is None:
            lockout = LoginLockout(
                scope=scope.value,
                identifier=identifier,
                locked_until=locked_until,
                lockout_count=1,
                created_at=now,
                updated_at=now,
            )
        else:
            lockout.lockout_count += 1
            lockout.locked_until = locked_until
            lockout.updated_at = now
        self._session.add(lockout)
        self._session.flush()
        return lockout

    def clear_lockout(self, scope: LoginScope, identifier: str) -> None:
        """Elimina el lockout del par, junto con su historial de backoff."""
        self._session.execute(
            delete(LoginLockout).where(
                LoginLockout.scope == scope.value,
                LoginLockout.identifier == identifier,
            )
        )
        self._session.flush()
