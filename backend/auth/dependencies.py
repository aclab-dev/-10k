"""Dependencia FastAPI que protege los endpoints sensibles del dashboard."""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status

from backend.auth.config import AuthCredentials, get_auth_credentials
from backend.auth.tokens import TokenClaims, TokenError, TokenExpired, verify_token

_log = structlog.get_logger()

_BEARER_PREFIX = "bearer "

# Identidad usada cuando la auth está deshabilitada (solo desarrollo local).
_ANONYMOUS = "anonymous"


def _unauthorized(detail: str) -> HTTPException:
    """401 con el header que el cliente necesita para saber cómo autenticarse."""
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization")
    if not header:
        raise _unauthorized("Falta el header Authorization")
    if not header.lower().startswith(_BEARER_PREFIX):
        raise _unauthorized("El esquema de autenticación debe ser Bearer")
    token = header[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise _unauthorized("Token vacío")
    return token


def require_auth(
    request: Request,
    credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
) -> TokenClaims:
    """Exige un bearer token válido y devuelve sus claims.

    Con `dashboard_auth.enabled=false` (solo desarrollo local) deja pasar sin
    token y devuelve claims anónimos. El boot ya falló si la auth está
    habilitada y las credenciales están incompletas, así que acá no hay caso
    "habilitada pero mal configurada".
    """
    if not credentials.enabled:
        return TokenClaims.anonymous(_ANONYMOUS)

    token = _extract_bearer_token(request)
    try:
        claims = verify_token(token, secret_key=credentials.secret_key)
    except TokenExpired as exc:
        # No logueamos el token ni el header: el scrubber de core.logging los
        # enmascararía, pero lo más seguro es directamente no pasarlos.
        _log.info("auth.token_expired", path=request.url.path)
        raise _unauthorized("Token expirado") from exc
    except TokenError as exc:
        _log.warning("auth.token_invalid", path=request.url.path)
        raise _unauthorized("Token inválido") from exc

    return claims
