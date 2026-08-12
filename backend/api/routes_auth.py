"""Endpoints de autenticación del dashboard: login y verificación de sesión."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.config import (
    AuthCredentials,
    get_auth_credentials,
    get_login_throttle_config,
)
from backend.auth.dependencies import require_auth
from backend.auth.hashing import dummy_hash, verify_password
from backend.auth.throttle import LoginThrottle
from backend.auth.tokens import TokenClaims, issue_token
from backend.core.config import LoginThrottleConfig
from backend.storage.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_log = structlog.get_logger()

# Mensaje único para usuario inexistente y password incorrecta: distinguirlos
# le confirmaría a un atacante qué usuario existe.
_INVALID_CREDENTIALS = "Usuario o contraseña incorrectos"

# Tampoco distingue scope: decir "esta IP" o "este usuario" describiría el
# estado del throttle a quien lo está sondeando.
_TOO_MANY_ATTEMPTS = "Demasiados intentos fallidos. Probá de nuevo más tarde."


def _client_ip(request: Request) -> str | None:
    """IP de origen de la request.

    Se lee de `request.client`, que uvicorn ya resuelve desde `X-Forwarded-For`
    cuando se corre con `--proxy-headers`. Leer el header acá sería peor: sin
    proxy de confianza adelante, el cliente lo elige y el límite por IP se evade
    cambiando un string.
    """
    return request.client.host if request.client else None


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
    request: Request,
    payload: LoginRequest,
    credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
    throttle_config: Annotated[LoginThrottleConfig, Depends(get_login_throttle_config)],
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    """Valida usuario y contraseña y emite un token de sesión.

    Siempre se verifica un hash, exista o no el usuario: si cortáramos temprano
    cuando el usuario no matchea, el tiempo de respuesta revelaría cuál es el
    usuario válido.

    El throttle (F15) se consulta antes de verificar: una identidad bloqueada
    responde 429 sin pagar scrypt, que cuesta ~100 ms y ~64 MB por intento.
    """
    if not credentials.enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "La autenticación del dashboard está deshabilitada",
        )

    now = datetime.now(UTC)
    ip = _client_ip(request)
    throttle = LoginThrottle(db, throttle_config)

    decision = throttle.check_lockout(payload.username, ip, now=now)
    if decision.locked:
        _log.warning(
            "auth.login_throttled",
            scope=decision.scope.value if decision.scope else None,
            retry_after_seconds=decision.retry_after_seconds,
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            _TOO_MANY_ATTEMPTS,
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    # compare_digest sobre bytes, no str: con str lanza TypeError si la entrada
    # tiene caracteres no-ASCII, y la entrada la controla el cliente.
    username_ok = hmac.compare_digest(
        payload.username.encode("utf-8"), credentials.username.encode("utf-8")
    )
    expected_hash = credentials.password_hash if username_ok else dummy_hash()
    password_ok = verify_password(payload.password, expected_hash)

    if not (username_ok and password_ok):
        throttle.record_failure(payload.username, ip, now=now)
        # Commit antes del raise: el 401 no puede perder el conteo del intento.
        db.commit()
        _log.warning("auth.login_failed", username=payload.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_CREDENTIALS)

    throttle.clear(payload.username, ip)
    db.commit()

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
