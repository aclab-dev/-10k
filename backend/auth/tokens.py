"""Tokens de sesión opacos firmados con HMAC-SHA256 (stdlib) — sin dependencias externas.

Formato: `<payload_b64url>.<signature_b64url>`, donde el payload es JSON compacto
con `sub` (usuario), `iat`, `exp` y `jti` (UUID). Es stateless y verificable sin
DB: el server no guarda sesiones, la firma y el `exp` son toda la autoridad.

El `jti` no se usa para revocar (no hay denylist en esta fase); está para que
dos tokens emitidos en el mismo segundo sean distintos y para poder correlacionar
una sesión en los logs de auditoría sin loguear el token en sí.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


class TokenError(Exception):
    """Base de los errores de verificación de token."""


class TokenInvalid(TokenError):
    """El token está malformado o su firma no corresponde a la secret key."""


class TokenExpired(TokenError):
    """El token está bien firmado pero su `exp` ya pasó."""


@dataclass(frozen=True)
class TokenClaims:
    """Claims verificados de un token de sesión."""

    sub: str
    issued_at: datetime
    expires_at: datetime
    jti: str

    @classmethod
    def anonymous(cls, sub: str, *, now: datetime | None = None) -> TokenClaims:
        """Claims sintéticos para cuando la auth está deshabilitada.

        No provienen de ningún token firmado y nunca se emiten al cliente:
        existen solo para que los endpoints protegidos reciban siempre la misma
        forma de identidad y no tengan que ramificar por `None`.
        """
        issued_at = now or datetime.now(UTC)
        return cls(sub=sub, issued_at=issued_at, expires_at=issued_at, jti=str(uuid.uuid4()))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload_b64: str, secret_key: str) -> str:
    digest = hmac.new(
        secret_key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64encode(digest)


def issue_token(
    username: str,
    *,
    secret_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> tuple[str, TokenClaims]:
    """Emite un token firmado para `username` y devuelve `(token, claims)`.

    `now` es inyectable para que los tests puedan emitir tokens ya expirados sin
    dormir ni parchear el reloj global.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    claims = TokenClaims(
        sub=username,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=str(uuid.uuid4()),
    )
    payload = {
        "sub": claims.sub,
        "iat": int(claims.issued_at.timestamp()),
        "exp": int(claims.expires_at.timestamp()),
        "jti": claims.jti,
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret_key)}", claims


def verify_token(token: str, *, secret_key: str, now: datetime | None = None) -> TokenClaims:
    """Verifica firma y expiración, y devuelve los claims.

    Lanza `TokenInvalid` si el formato o la firma no cierran, y `TokenExpired`
    si la firma es válida pero el token venció. La firma se chequea ANTES de
    mirar el `exp`: los claims de un token sin firmar no son confiables.
    """
    # Un token legítimo es base64url puro, o sea ASCII. Hay que rechazar lo que
    # no lo sea ANTES de tocarlo: `hmac.compare_digest` sobre str lanza
    # TypeError con caracteres no-ASCII, y el header lo controla el cliente
    # (Starlette lo decodifica en latin-1, así que cualquier byte llega acá).
    # Sin este chequeo, `Authorization: Bearer a.<0xF1>` da 500 en vez de 401.
    if not token.isascii():
        raise TokenInvalid("Token con caracteres no-ASCII")

    payload_b64, separator, signature_b64 = token.partition(".")
    if not separator or not payload_b64 or not signature_b64:
        raise TokenInvalid("Formato de token inválido")

    if not hmac.compare_digest(_sign(payload_b64, secret_key), signature_b64):
        raise TokenInvalid("Firma de token inválida")

    try:
        payload: Any = json.loads(_b64decode(payload_b64))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenInvalid("Payload de token ilegible") from exc

    if not isinstance(payload, dict):
        raise TokenInvalid("Payload de token no es un objeto")

    try:
        sub = payload["sub"]
        issued_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
        expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
        jti = payload["jti"]
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
        raise TokenInvalid("Claims de token incompletos o inválidos") from exc

    if not isinstance(sub, str) or not isinstance(jti, str):
        raise TokenInvalid("Claims de token con tipos inesperados")

    if expires_at <= (now or datetime.now(UTC)):
        raise TokenExpired("Token expirado")

    return TokenClaims(sub=sub, issued_at=issued_at, expires_at=expires_at, jti=jti)
