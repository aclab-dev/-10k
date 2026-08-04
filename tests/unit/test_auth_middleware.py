"""Tests del wiring de auth: qué rutas exigen token y cuáles quedan públicas.

Complementa test_routes_auth.py, que cubre el login en sí. Acá se verifica que
la dependencia esté efectivamente colgada de los routers sensibles — el bug que
importa es olvidarse de proteger uno.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.auth.config import get_auth_credentials
from backend.storage.database import get_db
from tests.unit.conftest import make_auth_credentials, make_bot_run

SENSITIVE_PATHS = [
    "/api/status",
    "/api/status/history",
    "/api/decisions",
    "/api/risk/validations",
    "/api/tokens/usage",
    "/api/tokens/budget",
]

PUBLIC_PATHS = ["/health"]


@pytest.fixture
def auth_disabled_client(session: Session) -> Generator[TestClient, None, None]:
    def _get_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_auth_credentials] = lambda: make_auth_credentials(enabled=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_sensitive_endpoints_reject_anonymous_requests(
    anon_client: TestClient, session: Session, path: str
) -> None:
    """401 incluso con datos cargados: la auth corre antes que la lógica."""
    make_bot_run(session)
    response = anon_client.get(path)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_sensitive_endpoints_answer_with_a_valid_token(
    client: TestClient, session: Session, path: str
) -> None:
    make_bot_run(session)
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_sensitive_endpoints_reject_a_garbage_token(anon_client: TestClient, path: str) -> None:
    response = anon_client.get(path, headers={"Authorization": "Bearer basura.basura"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_auth_runs_before_the_404_of_a_missing_bot_run(anon_client: TestClient, path: str) -> None:
    """Sin bot run activo y sin token: 401, no 404.

    Si respondiera 404 le estaría confirmando a un anónimo que no hay ningún bot
    corriendo — una filtración chica pero gratuita de evitar.
    """
    assert anon_client.get(path).status_code == 401


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_endpoints_stay_open(anon_client: TestClient, path: str) -> None:
    """/health lo consume el healthcheck de docker-compose sin credenciales."""
    assert anon_client.get(path).status_code == 200


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
def test_sensitive_endpoints_are_open_when_auth_is_disabled(
    auth_disabled_client: TestClient, session: Session, path: str
) -> None:
    make_bot_run(session)
    assert auth_disabled_client.get(path).status_code == 200
