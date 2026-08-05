"""Credenciales del dashboard, leídas exclusivamente de env vars.

Deliberadamente separado de `backend.core.config`: `AppConfig` se serializa con
`model_dump()` hacia `bot_runs.config_snapshot`, así que ningún secreto puede
ser campo de ese modelo. Acá viven solo en memoria y nunca se serializan.

La validación fail-closed también vive acá y no en `load_config`: el worker y los
scripts cargan la config y no tienen por qué conocer las credenciales del
dashboard. Esta función la llama solo la API — al boot desde el lifespan y en
cada request protegida (cacheada, así que valida una sola vez por proceso).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from backend.auth.hashing import validate_password_hash
from backend.core.config import ConfigError, get_config

_REQUIRED_ENV_VARS = ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD_HASH", "DASHBOARD_SECRET_KEY")

# 32 chars ~= 192 bits con token_urlsafe: margen de sobra para una clave HMAC-SHA256
# y suficiente para rechazar secrets de juguete tipo "changeme".
_MIN_SECRET_KEY_LENGTH = 32


@dataclass(frozen=True)
class AuthCredentials:
    """Credenciales y parámetros efectivos de la auth del dashboard."""

    enabled: bool
    username: str
    password_hash: str
    secret_key: str
    token_ttl_seconds: int

    def __repr__(self) -> str:
        """Repr sin secretos: evita filtrarlos en un traceback o un log de debug."""
        return (
            f"AuthCredentials(enabled={self.enabled!r}, username={self.username!r}, "
            f"password_hash='***', secret_key='***', "
            f"token_ttl_seconds={self.token_ttl_seconds!r})"
        )


def validate_credentials(credentials: AuthCredentials) -> None:
    """Fail-closed: con auth habilitada, las credenciales deben estar completas.

    Preferimos que la API no levante a que levante con los endpoints del
    dashboard abiertos. Los mensajes dicen exactamente qué falta y cómo generarlo.
    """
    if not credentials.enabled:
        return

    missing = [
        name
        for name, value in zip(
            _REQUIRED_ENV_VARS,
            (credentials.username, credentials.password_hash, credentials.secret_key),
            strict=True,
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"dashboard_auth.enabled=true requiere {', '.join(missing)} en el entorno. "
            "Generar el hash y la secret key con `python scripts/hash_password.py`. "
            "Para deshabilitar la auth (solo desarrollo local), setear "
            "BOT__DASHBOARD_AUTH__ENABLED=false."
        )

    if not validate_password_hash(credentials.password_hash):
        raise ConfigError(
            "DASHBOARD_PASSWORD_HASH tiene un formato inválido. Regenerarlo con "
            "`python scripts/hash_password.py`."
        )

    if len(credentials.secret_key) < _MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"DASHBOARD_SECRET_KEY debe tener al menos {_MIN_SECRET_KEY_LENGTH} caracteres. "
            "Generarla con `python scripts/hash_password.py`."
        )


@lru_cache(maxsize=1)
def get_auth_credentials() -> AuthCredentials:
    """Credenciales efectivas del dashboard, validadas. Cacheado como el resto de la config."""
    cfg = get_config()
    credentials = AuthCredentials(
        enabled=cfg.dashboard_auth.enabled,
        username=os.environ.get("DASHBOARD_USERNAME", ""),
        password_hash=os.environ.get("DASHBOARD_PASSWORD_HASH", ""),
        secret_key=os.environ.get("DASHBOARD_SECRET_KEY", ""),
        token_ttl_seconds=cfg.dashboard_auth.token_ttl_seconds,
    )
    validate_credentials(credentials)
    return credentials
