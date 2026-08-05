"""Tests de backend/auth/config.py — validación fail-closed de credenciales."""

from __future__ import annotations

import pytest

from backend.auth.config import AuthCredentials, validate_credentials
from backend.auth.hashing import hash_password
from backend.core.config import ConfigError

VALID_HASH = hash_password("password-de-test", n=2**4, r=1, p=1)
VALID_SECRET = "secret-key-de-test-con-mas-de-32-caracteres"


def credentials(**overrides: object) -> AuthCredentials:
    defaults: dict[str, object] = {
        "enabled": True,
        "username": "admin",
        "password_hash": VALID_HASH,
        "secret_key": VALID_SECRET,
        "token_ttl_seconds": 3600,
    }
    return AuthCredentials(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_complete_credentials_pass() -> None:
    validate_credentials(credentials())


@pytest.mark.parametrize(
    ("field", "env_var"),
    [
        ("username", "DASHBOARD_USERNAME"),
        ("password_hash", "DASHBOARD_PASSWORD_HASH"),
        ("secret_key", "DASHBOARD_SECRET_KEY"),
    ],
)
def test_missing_credential_fails_naming_the_env_var(field: str, env_var: str) -> None:
    with pytest.raises(ConfigError, match=env_var):
        validate_credentials(credentials(**{field: ""}))


def test_error_lists_every_missing_credential_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_credentials(credentials(username="", password_hash="", secret_key=""))
    message = str(exc.value)
    assert "DASHBOARD_USERNAME" in message
    assert "DASHBOARD_PASSWORD_HASH" in message
    assert "DASHBOARD_SECRET_KEY" in message


def test_malformed_password_hash_fails() -> None:
    with pytest.raises(ConfigError, match="formato inválido"):
        validate_credentials(credentials(password_hash="no-es-un-hash-scrypt"))


def test_short_secret_key_fails() -> None:
    with pytest.raises(ConfigError, match="al menos 32 caracteres"):
        validate_credentials(credentials(secret_key="changeme"))


def test_secret_key_of_exactly_the_minimum_length_passes() -> None:
    validate_credentials(credentials(secret_key="x" * 32))


def test_disabled_auth_needs_no_credentials() -> None:
    """Con la auth apagada no hay nada que validar — es el modo de desarrollo local."""
    validate_credentials(credentials(enabled=False, username="", password_hash="", secret_key=""))


def test_repr_hides_the_secrets() -> None:
    """Un traceback o un log de debug no puede exponer el hash ni la secret key."""
    rendered = repr(credentials())
    assert VALID_HASH not in rendered
    assert VALID_SECRET not in rendered
    assert "admin" in rendered
