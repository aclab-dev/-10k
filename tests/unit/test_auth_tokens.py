"""Tests de backend/auth/tokens.py — emisión y verificación de tokens de sesión."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.auth.tokens import (
    TokenExpired,
    TokenInvalid,
    issue_token,
    verify_token,
)

SECRET = "secret-key-de-test-con-mas-de-32-caracteres"
OTHER_SECRET = "otra-secret-key-de-test-con-mas-de-32-chars"
TTL = 3600


def test_issued_token_verifies_and_carries_the_username() -> None:
    token, claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL)
    verified = verify_token(token, secret_key=SECRET)
    assert verified.sub == "agus"
    assert verified.jti == claims.jti


def test_expiration_matches_the_configured_ttl() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    _token, claims = issue_token("agus", secret_key=SECRET, ttl_seconds=900, now=now)
    assert claims.issued_at == now
    assert claims.expires_at == now + timedelta(seconds=900)


def test_expired_token_raises_token_expired() -> None:
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    token, _claims = issue_token("agus", secret_key=SECRET, ttl_seconds=60, now=issued_at)
    with pytest.raises(TokenExpired):
        verify_token(token, secret_key=SECRET)


def test_token_is_valid_right_up_to_its_expiry() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    token, claims = issue_token("agus", secret_key=SECRET, ttl_seconds=60, now=now)
    assert verify_token(token, secret_key=SECRET, now=claims.expires_at - timedelta(seconds=1))
    with pytest.raises(TokenExpired):
        verify_token(token, secret_key=SECRET, now=claims.expires_at)


def test_token_signed_with_another_secret_is_invalid() -> None:
    token, _claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL)
    with pytest.raises(TokenInvalid):
        verify_token(token, secret_key=OTHER_SECRET)


def test_tampered_signature_is_invalid() -> None:
    token, _claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL)
    payload_b64, _, signature = token.partition(".")
    tampered = f"{payload_b64}.{'A' * len(signature)}"
    with pytest.raises(TokenInvalid):
        verify_token(tampered, secret_key=SECRET)


def test_tampered_payload_is_invalid() -> None:
    """Cambiar el `sub` sin poder re-firmar no escala privilegios."""
    _token, _claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL)
    forged_payload = json.dumps(
        {
            "sub": "root",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "forjado",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    forged_b64 = base64.urlsafe_b64encode(forged_payload).decode("ascii").rstrip("=")
    _original_payload, _, signature = _token.partition(".")
    with pytest.raises(TokenInvalid):
        verify_token(f"{forged_b64}.{signature}", secret_key=SECRET)


def test_expiry_is_only_checked_after_the_signature() -> None:
    """Un token expirado Y mal firmado reporta firma inválida, no expiración."""
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    token, _claims = issue_token("agus", secret_key=SECRET, ttl_seconds=60, now=issued_at)
    with pytest.raises(TokenInvalid):
        verify_token(token, secret_key=OTHER_SECRET)


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "sin-punto",
        ".solo-firma",
        "solo-payload.",
        "..",
    ],
)
def test_malformed_token_raises_token_invalid(malformed: str) -> None:
    with pytest.raises(TokenInvalid):
        verify_token(malformed, secret_key=SECRET)


def test_correctly_signed_but_unreadable_payload_is_invalid() -> None:
    """Firma válida sobre un payload que no es JSON: sigue siendo inválido."""
    from backend.auth.tokens import _sign

    payload_b64 = base64.urlsafe_b64encode(b"esto-no-es-json").decode("ascii").rstrip("=")
    token = f"{payload_b64}.{_sign(payload_b64, SECRET)}"
    with pytest.raises(TokenInvalid):
        verify_token(token, secret_key=SECRET)


@pytest.mark.parametrize(
    "payload",
    [
        {"iat": 0, "exp": 9999999999, "jti": "x"},  # falta sub
        {"sub": "a", "exp": 9999999999, "jti": "x"},  # falta iat
        {"sub": "a", "iat": 0, "jti": "x"},  # falta exp
        {"sub": "a", "iat": 0, "exp": 9999999999},  # falta jti
        {"sub": 42, "iat": 0, "exp": 9999999999, "jti": "x"},  # sub no es str
        {"sub": "a", "iat": 0, "exp": "mañana", "jti": "x"},  # exp no es número
        [1, 2, 3],  # ni siquiera es un objeto
    ],
)
def test_signed_payload_with_bad_claims_is_invalid(payload: object) -> None:
    from backend.auth.tokens import _sign

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = f"{payload_b64}.{_sign(payload_b64, SECRET)}"
    with pytest.raises(TokenInvalid):
        verify_token(token, secret_key=SECRET)


def test_two_tokens_for_the_same_user_have_different_jti() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    _first, first_claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL, now=now)
    _second, second_claims = issue_token("agus", secret_key=SECRET, ttl_seconds=TTL, now=now)
    assert first_claims.jti != second_claims.jti
