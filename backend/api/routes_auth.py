"""Endpoints de autenticación del dashboard: login y verificación de sesión."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth.config import AuthCredentials, get_auth_credentials
from backend.auth.dependencies import require_auth
from backend.auth.hashing import dummy_hash, verify_password
from backend.auth.tokens import TokenClaims, issue_token

router = APIRouter(prefix="/auth", tags=["auth"])

_log = structlog.get_logger()

# Mensaje único para usuario inexistente y password incorrecta: distinguirlos
# le confirmaría a un atacante qué usuario existe.
_INVALID_CREDENTIALS = "Usuario o contraseña incorrectos"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class SessionOut(BaseModel):
    username: str
    issued_at: datetime
    expires_at: datetime


@router.post("/login")
def login(
    payload: LoginRequest,
    credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
) -> LoginResponse:
    """Valida usuario y contraseña y emite un token de sesión.

    Siempre se verifica un hash, exista o no el usuario: si cortáramos temprano
    cuando el usuario no matchea, el tiempo de respuesta revelaría cuál es el
    usuario válido.
    """
    if not credentials.enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "La autenticación del dashboard está deshabilitada",
        )

    # compare_digest sobre bytes, no str: con str lanza TypeError si la entrada
    # tiene caracteres no-ASCII, y la entrada la controla el cliente.
    username_ok = hmac.compare_digest(
        payload.username.encode("utf-8"), credentials.username.encode("utf-8")
    )
    expected_hash = credentials.password_hash if username_ok else dummy_hash()
    password_ok = verify_password(payload.password, expected_hash)

    if not (username_ok and password_ok):
        _log.warning("auth.login_failed", username=payload.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_CREDENTIALS)

    token, claims = issue_token(
        credentials.username,
        secret_key=credentials.secret_key,
        ttl_seconds=credentials.token_ttl_seconds,
    )
    _log.info("auth.login_succeeded", username=credentials.username, jti=claims.jti)
    return LoginResponse(access_token=token, expires_at=claims.expires_at)


@router.get("/me")
def me(claims: Annotated[TokenClaims, Depends(require_auth)]) -> SessionOut:
    """Devuelve la sesión activa. Sirve al frontend para validar el token guardado."""
    return SessionOut(
        username=claims.sub,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )
