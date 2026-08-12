"""Rate limiting y lockout de brute-force del login del dashboard (F15).

El endpoint de login es el único de la API que un anónimo puede golpear, y cada
intento cuesta una derivación scrypt (~100 ms, ~64 MB): sin límite es a la vez un
oráculo de fuerza bruta y un DoS barato. Este módulo cuenta los fallos en una
ventana deslizante por dos scopes independientes —el usuario tipeado y la IP de
origen— y bloquea temporalmente con backoff exponencial ante la reincidencia.

El orden importa: `check_lockout` corre **antes** de verificar la password, así
una identidad ya bloqueada cuesta un SELECT indexado y no paga scrypt ni escribe.

Todo el estado vive en Postgres (ver `backend.storage.models`), no en memoria:
tiene que ser compartido entre workers y sobrevivir a un reinicio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from backend.core.config import LoginThrottleConfig
from backend.storage.models import LoginScope
from backend.storage.repositories.auth import LoginThrottleRepository

_log = structlog.get_logger()

# Postgres corta `identifier` en 255; el username ya viene acotado a 128 por el
# schema del request, pero truncamos igual para no depender de esa validación.
_MAX_IDENTIFIER_LENGTH = 255

# Identidad usada cuando la request no expone IP de origen (p. ej. un TestClient
# o un transporte sin peer). Se agrupan todas juntas en vez de saltearse el
# límite: preferimos un falso positivo a un bypass.
UNKNOWN_IP = "unknown"


@dataclass(frozen=True)
class ThrottleDecision:
    """Resultado del chequeo previo al login."""

    locked: bool
    retry_after_seconds: int
    scope: LoginScope | None = None


def normalize_username(username: str) -> str:
    """Clave de conteo del scope USERNAME.

    `casefold` evita que alternar mayúsculas multiplique la cuota. No afecta la
    comparación real de credenciales, que sigue siendo exacta en `routes_auth`.
    """
    return username.casefold().strip()[:_MAX_IDENTIFIER_LENGTH]


def normalize_ip(ip: str | None) -> str:
    """Clave de conteo del scope IP, con fallback explícito si no hay peer."""
    if not ip:
        return UNKNOWN_IP
    return ip.strip()[:_MAX_IDENTIFIER_LENGTH] or UNKNOWN_IP


def compute_lockout_seconds(lockout_count: int, config: LoginThrottleConfig) -> int:
    """Duración del bloqueo número `lockout_count` (1-based), con techo.

    El primer bloqueo dura `lockout_seconds` y cada reincidencia lo multiplica
    por `lockout_backoff_factor`, hasta `max_lockout_seconds`.
    """
    if lockout_count < 1:
        raise ValueError(f"lockout_count debe ser >= 1, recibido: {lockout_count}")

    if config.lockout_backoff_factor <= 1.0:
        return min(config.lockout_seconds, config.max_lockout_seconds)

    # `lockout_count` no tiene techo: crece con cada reincidencia y solo lo
    # resetea un login exitoso. Calcular la potencia a ciegas desborda el float
    # mucho antes de que eso sea improbable, así que primero se descarta todo
    # exponente que ya supera el máximo.
    exponent = lockout_count - 1
    exponent_at_ceiling = math.log(config.max_lockout_seconds / config.lockout_seconds) / math.log(
        config.lockout_backoff_factor
    )
    if exponent >= exponent_at_ceiling:
        return config.max_lockout_seconds

    seconds = config.lockout_seconds * (config.lockout_backoff_factor**exponent)
    return min(int(seconds), config.max_lockout_seconds)


def _remaining_seconds(locked_until: datetime, now: datetime) -> int:
    """Segundos que faltan para la liberación, redondeados hacia arriba.

    Hacia arriba y con piso 1: un `Retry-After: 0` invitaría a reintentar de
    inmediato y volver a chocar contra el mismo 429.
    """
    remaining = (locked_until - now).total_seconds()
    return max(1, math.ceil(remaining))


def _as_utc(value: datetime) -> datetime:
    """Normaliza a UTC aware.

    SQLite (usado en los tests) devuelve datetimes naive aunque la columna sea
    TIMESTAMPTZ; Postgres los devuelve aware. Comparar ambos sin esto explota.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class LoginThrottle:
    """Aplica el rate limiting del login sobre una sesión de base de datos."""

    def __init__(self, session: Session, config: LoginThrottleConfig) -> None:
        self._repo = LoginThrottleRepository(session)
        self._config = config

    def _max_failures(self, scope: LoginScope) -> int:
        if scope is LoginScope.USERNAME:
            return self._config.max_failures_per_username
        return self._config.max_failures_per_ip

    def check_lockout(self, username: str, ip: str | None, *, now: datetime) -> ThrottleDecision:
        """Indica si la identidad está bloqueada, sin tocar los contadores.

        Se llama antes de verificar la password: el objetivo es no pagar scrypt
        por una identidad ya bloqueada.
        """
        if not self._config.enabled:
            return ThrottleDecision(locked=False, retry_after_seconds=0)

        identities = self._identities(username, ip)
        # Una sola query para los dos scopes: esto corre en cada login, también
        # en los exitosos.
        lockouts = self._repo.get_lockouts(identities)

        active = [
            (scope, _as_utc(lockout.locked_until))
            for scope, identifier in identities
            if (lockout := lockouts.get((scope, identifier))) is not None
            and _as_utc(lockout.locked_until) > now
        ]
        if not active:
            return ThrottleDecision(locked=False, retry_after_seconds=0)

        # Gana el bloqueo más largo, no el primero: reportar el más corto haría
        # que el cliente espere, reintente y se choque con el otro, todavía
        # vigente. `max` conserva el primero ante empate, así que un empate
        # sigue reportando USERNAME.
        scope, locked_until = max(active, key=lambda item: item[1])
        return ThrottleDecision(
            locked=True,
            retry_after_seconds=_remaining_seconds(locked_until, now),
            scope=scope,
        )

    def record_failure(self, username: str, ip: str | None, *, now: datetime) -> None:
        """Registra un intento fallido y bloquea si se alcanzó el umbral.

        Al bloquear se consumen los intentos contados: sin eso, al vencer el
        lockout los fallos que siguen dentro de la ventana volverían a bloquear
        con el primer intento nuevo.

        **Límite conocido**: contar y decidir no es atómico. Bajo READ COMMITTED,
        varias requests concurrentes pueden leer el conteo antes de que las otras
        commiteen su INSERT, y pasar el umbral sin que ninguna cree el lockout.
        El exceso está acotado por las requests en vuelo —cada una paga scrypt
        antes de contar, así que la concurrencia real la limita el pool de
        threads y la memoria— y se corrige sola: la primera request posterior a
        esos commits ve el conteo completo y bloquea. Cerrarlo del todo pide un
        lock por identificador (`pg_advisory_xact_lock`), que es específico de
        Postgres y quedaría sin cubrir por los tests, que corren en SQLite.
        """
        if not self._config.enabled:
            return

        window_start = now - timedelta(seconds=self._config.window_seconds)
        self._repo.purge_attempts_before(window_start)
        self._repo.purge_lockouts_before(self._backoff_memory_start(now))

        for scope, identifier in self._identities(username, ip):
            self._repo.record_failure(scope, identifier, now=now)
            failures = self._repo.count_failures_since(scope, identifier, window_start)
            if failures < self._max_failures(scope):
                continue

            previous = self._repo.get_lockout(scope, identifier)
            next_count = 1 if previous is None else previous.lockout_count + 1
            duration = compute_lockout_seconds(next_count, self._config)
            self._repo.upsert_lockout(
                scope,
                identifier,
                locked_until=now + timedelta(seconds=duration),
                now=now,
            )
            self._repo.clear_attempts(scope, identifier)

            # Evento auditable. `identifier` es el usuario tipeado o la IP: no
            # hay secretos acá, y sin el identificador el evento no sirve para
            # investigar el ataque.
            _log.warning(
                "auth.login_locked_out",
                scope=scope.value,
                identifier=identifier,
                failures=failures,
                lockout_count=next_count,
                lockout_seconds=duration,
            )

    def _backoff_memory_start(self, now: datetime) -> datetime:
        """Momento antes del cual un lockout vencido ya no aporta nada.

        Un lockout vencido sigue siendo útil mientras su `lockout_count` pueda
        castigar una reincidencia. Se conserva por el mayor entre la ventana de
        conteo y el bloqueo más largo posible: quien no reintentó en todo ese
        tiempo es, a los fines del backoff, un ofensor nuevo.
        """
        retention = max(self._config.window_seconds, self._config.max_lockout_seconds)
        return now - timedelta(seconds=retention)

    def clear(self, username: str, ip: str | None) -> None:
        """Resetea contadores y backoff tras un login exitoso."""
        if not self._config.enabled:
            return

        for scope, identifier in self._identities(username, ip):
            self._repo.clear_attempts(scope, identifier)
            self._repo.clear_lockout(scope, identifier)

    @staticmethod
    def _identities(username: str, ip: str | None) -> tuple[tuple[LoginScope, str], ...]:
        """Los dos pares (scope, identificador) que se cuentan por intento."""
        return (
            (LoginScope.USERNAME, normalize_username(username)),
            (LoginScope.IP, normalize_ip(ip)),
        )
