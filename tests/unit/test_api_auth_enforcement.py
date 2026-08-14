"""Tests de que los endpoints sensibles del dashboard exigen bearer token.

Dos capas: un test parametrizado que golpea cada endpoint sin credenciales y
espera 401 (comportamiento real, end-to-end), y un test estructural que
recorre `app.routes` y falla si un router nuevo bajo `/api` se registra sin
`require_auth` — que es exactamente el modo de fallo que un test por-endpoint
no detecta (nadie escribe el test 401 para la ruta que se olvidó de proteger).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.main import app
from tests.unit.conftest import make_bot_run

# Rutas bajo /api explícitamente públicas (pre-sesión o healthcheck) — no deben
# llevar require_auth. Cualquier otra ruta /api/* se espera protegida.
_PUBLIC_API_PATHS = {"/api/auth/login"}

_PROTECTED_ENDPOINTS = [
    ("GET", "/api/status", None),
    ("GET", "/api/status/history", None),
    ("GET", "/api/decisions", None),
    ("GET", "/api/decisions/00000000-0000-0000-0000-000000000000", None),
    ("GET", "/api/risk/validations", None),
    ("GET", "/api/tokens/usage", None),
    ("GET", "/api/tokens/budget", None),
    ("POST", "/api/kill-switch", {"reason": "test"}),
]


@pytest.mark.parametrize("method,path,json_body", _PROTECTED_ENDPOINTS)
def test_endpoint_401_without_credentials(
    anon_client: TestClient,
    session: Session,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    make_bot_run(session)
    response = anon_client.request(method, path, json=json_body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_all_dashboard_routes_require_auth() -> None:
    """Recorre las rutas registradas: toda ruta /api/* no pública debe depender de require_auth.

    Estructural a propósito: un router del dashboard registrado sin pasar por
    `_protected` en `include_router` (backend/app/main.py) queda público en
    silencio y ningún test por-endpoint se entera, porque nadie escribe el
    test 401 de una ruta que no sabe que existe.
    """
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or not path.startswith("/api") or path in _PUBLIC_API_PATHS:
            continue
        dependant_names = {dep.call.__name__ for dep in route.dependant.dependencies}
        assert "require_auth" in dependant_names, f"{path} no exige require_auth"
        checked += 1

    assert checked >= len(_PROTECTED_ENDPOINTS)
