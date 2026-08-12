"""Repositorio del throttling de login (F15): conteo de fallos y estado de lockout.

Un solo repositorio para las dos tablas porque siempre se usan juntas: el conteo
de `login_attempts` es lo que decide si hay que escribir un `login_lockouts`, y
un login exitoso limpia ambas. Separarlos obligaría al caller a coordinar dos
objetos para cada operación.

Como el resto de los repos, no commitea: eso lo decide el caller.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.storage.models import LoginAttempt, LoginLockout, LoginScope
from backend.storage.repositories.base import BaseRepository

# Primera clave del advisory lock: separa los espacios de nombres de cada scope.
_LOCK_NAMESPACE = {LoginScope.USERNAME: 1, LoginScope.IP: 2}

_INT32_RANGE = 2**32
_INT32_MAX = 2**31


def _advisory_key(identifier: str) -> int:
    """Deriva la segunda clave del lock, un int32 con signo.

    Se hashea en Python y no con `hashtext` de Postgres para no depender de una
    función interna sin contrato de estabilidad. Una colisión solo serializa de
    más a dos identidades: es correcto igual, apenas un poco menos paralelo.
    """
    key = zlib.crc32(identifier.encode("utf-8"))
    return key - _INT32_RANGE if key >= _INT32_MAX else key


class LoginThrottleRepository(BaseRepository[LoginAttempt]):
    model = LoginAttempt

    # ------------------------------------------------------------------
    # Serialización por identidad
    # ------------------------------------------------------------------

    def lock_identity(self, scope: LoginScope, identifier: str) -> None:
        """Serializa el conteo y la decisión de bloqueo de una identidad.

        Contar los fallos y decidir si se bloquea son dos pasos: bajo READ
        COMMITTED, varias requests concurrentes leen el conteo antes de que las
        otras commiteen y pasan el umbral sin que ninguna cree el lockout. El
        lock advisory serializa esa sección crítica por identidad.

        Se toma después de scrypt y se libera al terminar la transacción, que el
        endpoint commitea enseguida: no hay derivación de clave adentro del lock.

        Solo aplica en PostgreSQL, que es la base del proyecto; en otros
        dialectos (SQLite en los tests unitarios) es un no-op y la serialización
        la da el propio test, que es single-thread. La cobertura real está en
        `tests/integration/test_login_throttle_concurrency.py`.
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return

        # Namespace por scope en la primera clave: así un username y una IP que
        # hashean igual no se serializan entre sí. El orden de toma siempre es
        # USERNAME y después IP, y las dos claves viven en espacios disjuntos,
        # así que no puede formarse un ciclo de espera.
        self._session.execute(
            select(func.pg_advisory_xact_lock(_LOCK_NAMESPACE[scope], _advisory_key(identifier)))
        )

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

    def get_lockouts(
        self, pairs: Sequence[tuple[LoginScope, str]]
    ) -> dict[tuple[LoginScope, str], LoginLockout]:
        """Trae los lockouts de varios pares en una sola query.

        El chequeo previo al login mira los dos scopes en cada request, incluida
        la exitosa: con `get_lockout` eso son dos round trips en el path caliente.
        Se usa un OR de igualdades y no un `IN` de tuplas porque las row values
        no están en todos los dialectos que corren los tests.
        """
        if not pairs:
            return {}

        stmt = select(LoginLockout).where(
            or_(
                *(
                    and_(LoginLockout.scope == scope.value, LoginLockout.identifier == identifier)
                    for scope, identifier in pairs
                )
            )
        )
        return {(LoginScope(row.scope), row.identifier): row for row in self._session.scalars(stmt)}

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

        El INSERT va en un savepoint porque un brute-force es concurrente por
        definición: dos requests que cruzan el umbral a la vez intentan crear la
        misma fila y una choca contra el unique. Sin esto, ese choque saldría
        como 500 del login justo cuando el throttle importa.
        """
        existing = self.get_lockout(scope, identifier)
        if existing is not None:
            return self._renew(existing, locked_until=locked_until, now=now)

        lockout = LoginLockout(
            scope=scope.value,
            identifier=identifier,
            locked_until=locked_until,
            lockout_count=1,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(lockout)
        except IntegrityError:
            concurrent = self.get_lockout(scope, identifier)
            if concurrent is None:
                # El unique no fue el de (scope, identifier): no es la carrera
                # que este except cubre y no hay nada que reintentar.
                raise
            return self._renew(concurrent, locked_until=locked_until, now=now)
        return lockout

    def _renew(
        self, lockout: LoginLockout, *, locked_until: datetime, now: datetime
    ) -> LoginLockout:
        lockout.lockout_count += 1
        lockout.locked_until = locked_until
        lockout.updated_at = now
        self._session.flush()
        return lockout

    def purge_lockouts_before(self, cutoff: datetime) -> None:
        """Descarta lockouts vencidos hace rato, con su historial de backoff.

        Sin esto la tabla crece sin techo: un escaneo desde muchas IPs deja una
        fila por IP y ninguna se limpia nunca, porque `clear_lockout` solo corre
        tras un login exitoso que esas IPs no van a hacer.

        El `cutoff` lo elige el caller para no borrar el `lockout_count` de quien
        todavía puede reincidir: perder esa memoria le devolvería el backoff
        mínimo a un atacante que insiste.
        """
        self._session.execute(delete(LoginLockout).where(LoginLockout.locked_until < cutoff))
        self._session.flush()

    def clear_lockout(self, scope: LoginScope, identifier: str) -> None:
        """Elimina el lockout del par, junto con su historial de backoff."""
        self._session.execute(
            delete(LoginLockout).where(
                LoginLockout.scope == scope.value,
                LoginLockout.identifier == identifier,
            )
        )
        self._session.flush()
