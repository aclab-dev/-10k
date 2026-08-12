"""Tests del rate limiting y lockout de brute-force del login (F15).

Acá se testea el servicio con el reloj inyectado, que es lo único que permite
verificar la expiración de la ventana y la liberación del lockout sin esperar.
Los tests del endpoint (429, Retry-After) están en test_routes_auth.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.auth.throttle import (
    UNKNOWN_IP,
    LoginThrottle,
    compute_lockout_seconds,
    normalize_ip,
    normalize_username,
)
from backend.core.config import ConfigError, LoginThrottleConfig
from backend.storage.models import LoginScope
from backend.storage.repositories.auth import LoginThrottleRepository

T0 = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
USER = "test-user"
IP = "203.0.113.7"


def make_config(**overrides: Any) -> LoginThrottleConfig:
    """Config chica y de números redondos: los umbrales reales harían tests lentos."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "max_failures_per_username": 3,
        "max_failures_per_ip": 100,  # alto salvo que el test mire el scope IP
        "window_seconds": 600,
        "lockout_seconds": 60,
        "lockout_backoff_factor": 2.0,
        "max_lockout_seconds": 300,
    }
    return LoginThrottleConfig(**{**defaults, **overrides})


def fail_n(
    throttle: LoginThrottle, n: int, *, at: datetime, user: str = USER, ip: str = IP
) -> None:
    for _ in range(n):
        throttle.record_failure(user, ip, now=at)


# ---------------------------------------------------------------------------
# Backoff (función pura)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lockout_count", "expected"),
    [(1, 60), (2, 120), (3, 240), (4, 300), (10, 300)],
)
def test_backoff_duplica_hasta_el_techo(lockout_count: int, expected: int) -> None:
    assert compute_lockout_seconds(lockout_count, make_config()) == expected


def test_backoff_factor_uno_mantiene_la_duracion_base() -> None:
    config = make_config(lockout_backoff_factor=1.0)
    assert [compute_lockout_seconds(n, config) for n in (1, 2, 5)] == [60, 60, 60]


def test_backoff_con_conteo_invalido_es_error_explicito() -> None:
    with pytest.raises(ValueError, match="lockout_count"):
        compute_lockout_seconds(0, make_config())


def test_backoff_no_desborda_con_factor_alto() -> None:
    """Un factor grande y muchas reincidencias no deben producir inf ni OverflowError."""
    config = make_config(lockout_backoff_factor=10.0, max_lockout_seconds=3600)
    assert compute_lockout_seconds(400, config) == 3600


# ---------------------------------------------------------------------------
# Normalización de identificadores
# ---------------------------------------------------------------------------


def test_username_normaliza_mayusculas() -> None:
    """Alternar mayúsculas no debe multiplicar la cuota."""
    assert normalize_username("Test-User") == normalize_username("test-user")


def test_ip_ausente_cae_en_un_identificador_explicito() -> None:
    assert normalize_ip(None) == UNKNOWN_IP
    assert normalize_ip("  ") == UNKNOWN_IP


# ---------------------------------------------------------------------------
# Conteo y lockout por usuario
# ---------------------------------------------------------------------------


def test_por_debajo_del_umbral_no_bloquea(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 2, at=T0)
    assert throttle.check_lockout(USER, IP, now=T0).locked is False


def test_bloquea_al_alcanzar_el_umbral_por_usuario(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    decision = throttle.check_lockout(USER, IP, now=T0)
    assert decision.locked is True
    assert decision.scope is LoginScope.USERNAME
    assert decision.retry_after_seconds == 60


def test_el_lockout_alcanza_a_cualquier_variante_de_mayusculas(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0, user="test-user")
    assert throttle.check_lockout("TEST-USER", IP, now=T0).locked is True


def test_un_usuario_bloqueado_no_bloquea_a_otro(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0, user="atacado")
    assert throttle.check_lockout("otro-usuario", "198.51.100.1", now=T0).locked is False


# ---------------------------------------------------------------------------
# Lockout por IP
# ---------------------------------------------------------------------------


def test_bloquea_por_ip_barriendo_usuarios_distintos(session: Session) -> None:
    """Un usuario distinto por intento nunca llega al umbral de usuario; la IP sí."""
    config = make_config(max_failures_per_ip=4)
    throttle = LoginThrottle(session, config)
    for i in range(4):
        throttle.record_failure(f"usuario-{i}", IP, now=T0)

    decision = throttle.check_lockout("usuario-nuevo", IP, now=T0)
    assert decision.locked is True
    assert decision.scope is LoginScope.IP


def test_el_bloqueo_de_una_ip_no_afecta_a_otra(session: Session) -> None:
    config = make_config(max_failures_per_ip=4)
    throttle = LoginThrottle(session, config)
    for i in range(4):
        throttle.record_failure(f"usuario-{i}", IP, now=T0)

    assert throttle.check_lockout("usuario-nuevo", "198.51.100.1", now=T0).locked is False


# ---------------------------------------------------------------------------
# Ventana deslizante
# ---------------------------------------------------------------------------


def test_los_fallos_viejos_salen_de_la_ventana(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 2, at=T0)

    fuera_de_ventana = T0 + timedelta(seconds=601)
    throttle.record_failure(USER, IP, now=fuera_de_ventana)

    assert throttle.check_lockout(USER, IP, now=fuera_de_ventana).locked is False


def test_los_fallos_viejos_se_purgan_de_la_tabla(session: Session) -> None:
    """Sin purga la tabla crece sin techo con identificadores que no vuelven."""
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 2, at=T0)

    fuera_de_ventana = T0 + timedelta(seconds=601)
    throttle.record_failure("otro-usuario", IP, now=fuera_de_ventana)

    repo = LoginThrottleRepository(session)
    assert repo.count_failures_since(LoginScope.USERNAME, USER, T0) == 0


def test_los_fallos_dentro_de_la_ventana_siguen_sumando(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 2, at=T0)

    casi_el_borde = T0 + timedelta(seconds=599)
    throttle.record_failure(USER, IP, now=casi_el_borde)

    assert throttle.check_lockout(USER, IP, now=casi_el_borde).locked is True


# ---------------------------------------------------------------------------
# Liberación y backoff acumulado
# ---------------------------------------------------------------------------


def test_el_lockout_sigue_vigente_antes_de_vencer(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    decision = throttle.check_lockout(USER, IP, now=T0 + timedelta(seconds=59))
    assert decision.locked is True
    assert decision.retry_after_seconds == 1


def test_el_lockout_se_libera_al_vencer(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    assert throttle.check_lockout(USER, IP, now=T0 + timedelta(seconds=61)).locked is False


def test_al_liberarse_el_usuario_recupera_la_cuota_completa(session: Session) -> None:
    """Los intentos que causaron el lockout se consumen: no re-bloquean al primer fallo."""
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    liberado = T0 + timedelta(seconds=61)
    throttle.record_failure(USER, IP, now=liberado)
    assert throttle.check_lockout(USER, IP, now=liberado).locked is False


def test_la_reincidencia_duplica_el_lockout(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    liberado = T0 + timedelta(seconds=61)
    fail_n(throttle, 3, at=liberado)

    decision = throttle.check_lockout(USER, IP, now=liberado)
    assert decision.locked is True
    assert decision.retry_after_seconds == 120


# ---------------------------------------------------------------------------
# Login exitoso
# ---------------------------------------------------------------------------


def test_el_login_exitoso_resetea_el_conteo(session: Session) -> None:
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 2, at=T0)

    throttle.clear(USER, IP)

    fail_n(throttle, 2, at=T0)
    assert throttle.check_lockout(USER, IP, now=T0).locked is False


def test_el_login_exitoso_resetea_el_backoff(session: Session) -> None:
    """Tras un login legítimo, un lockout futuro vuelve a durar el mínimo."""
    throttle = LoginThrottle(session, make_config())
    fail_n(throttle, 3, at=T0)

    liberado = T0 + timedelta(seconds=61)
    throttle.clear(USER, IP)
    fail_n(throttle, 3, at=liberado)

    assert throttle.check_lockout(USER, IP, now=liberado).retry_after_seconds == 60


# ---------------------------------------------------------------------------
# Throttle deshabilitado
# ---------------------------------------------------------------------------


def test_deshabilitado_nunca_bloquea(session: Session) -> None:
    throttle = LoginThrottle(session, make_config(enabled=False))
    fail_n(throttle, 20, at=T0)
    assert throttle.check_lockout(USER, IP, now=T0).locked is False


def test_deshabilitado_no_escribe_en_la_base(session: Session) -> None:
    throttle = LoginThrottle(session, make_config(enabled=False))
    fail_n(throttle, 5, at=T0)

    repo = LoginThrottleRepository(session)
    assert repo.count_failures_since(LoginScope.USERNAME, USER, T0) == 0
    assert repo.get_lockout(LoginScope.USERNAME, USER) is None


# ---------------------------------------------------------------------------
# Validación de la config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "max_failures_per_username",
        "max_failures_per_ip",
        "window_seconds",
        "lockout_seconds",
        "max_lockout_seconds",
    ],
)
def test_la_config_rechaza_valores_no_positivos(field: str) -> None:
    with pytest.raises((ConfigError, ValueError)):
        make_config(**{field: 0})


def test_la_config_rechaza_backoff_menor_a_uno() -> None:
    """Un factor <1 acortaría el lockout en cada reincidencia."""
    with pytest.raises((ConfigError, ValueError)):
        make_config(lockout_backoff_factor=0.5)


def test_la_config_rechaza_un_techo_menor_al_lockout_base() -> None:
    with pytest.raises((ConfigError, ValueError)):
        make_config(lockout_seconds=600, max_lockout_seconds=300)
