"""Tests de los endpoints POST /api/auth/login y GET /api/auth/me."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.auth.config import get_auth_credentials
from backend.auth.tokens import issue_token
from backend.storage.database import get_db
from tests.unit.conftest import (
    TEST_PASSWORD,
    TEST_SECRET_KEY,
    TEST_USERNAME,
    make_auth_credentials,
)


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_returns_a_usable_token(anon_client: TestClient) -> None:
    response = login(anon_client, TEST_USERNAME, TEST_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    me = anon_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == TEST_USERNAME


def test_login_response_never_echoes_the_password(anon_client: TestClient) -> None:
    response = login(anon_client, TEST_USERNAME, TEST_PASSWORD)
    assert TEST_PASSWORD not in response.text


def test_login_rejects_wrong_password(anon_client: TestClient) -> None:
    response = login(anon_client, TEST_USERNAME, "password-incorrecta")
    assert response.status_code == 401


def test_login_rejects_unknown_username(anon_client: TestClient) -> None:
    response = login(anon_client, "otro-usuario", TEST_PASSWORD)
    assert response.status_code == 401


def test_login_does_not_reveal_which_field_was_wrong(anon_client: TestClient) -> None:
    """Usuario inexistente y password incorrecta responden exactamente igual."""
    wrong_user = login(anon_client, "no-existe", TEST_PASSWORD)
    wrong_password = login(anon_client, TEST_USERNAME, "no-es-esta")
    assert wrong_user.status_code == wrong_password.status_code == 401
    assert wrong_user.json()["detail"] == wrong_password.json()["detail"]


@pytest.mark.parametrize("payload", [{}, {"username": TEST_USERNAME}, {"password": TEST_PASSWORD}])
def test_login_rejects_incomplete_payload(anon_client: TestClient, payload: dict) -> None:
    assert anon_client.post("/api/auth/login", json=payload).status_code == 422


@pytest.mark.parametrize("blank", ["", "   "])
def test_login_rejects_blank_credentials(anon_client: TestClient, blank: str) -> None:
    response = login(anon_client, blank, blank)
    assert response.status_code in (401, 422)


def test_me_requires_a_token(anon_client: TestClient) -> None:
    response = anon_client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "Bearer no-es-un-token",
        "Bearer a.b",
        "Basic dXNlcjpwYXNz",  # esquema equivocado
        "token-suelto-sin-esquema",
    ],
)
def test_me_rejects_bad_authorization_headers(anon_client: TestClient, header: str) -> None:
    response = anon_client.get("/api/auth/me", headers={"Authorization": header})
    assert response.status_code == 401


def test_me_accepts_the_bearer_scheme_case_insensitively(anon_client: TestClient) -> None:
    """RFC 7235: el esquema es case-insensitive."""
    token = login(anon_client, TEST_USERNAME, TEST_PASSWORD).json()["access_token"]
    response = anon_client.get("/api/auth/me", headers={"Authorization": f"bearer {token}"})
    assert response.status_code == 200


def test_me_rejects_an_expired_token(anon_client: TestClient) -> None:
    expired, _claims = issue_token(
        TEST_USERNAME,
        secret_key=TEST_SECRET_KEY,
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    response = anon_client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_me_rejects_a_token_signed_with_another_secret(anon_client: TestClient) -> None:
    forged, _claims = issue_token(
        TEST_USERNAME,
        secret_key="secret-key-de-un-atacante-con-32-caracteres",
        ttl_seconds=3600,
    )
    response = anon_client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_me_reports_the_session_window(client: TestClient) -> None:
    body = client.get("/api/auth/me").json()
    assert datetime.fromisoformat(body["expires_at"]) > datetime.fromisoformat(body["issued_at"])


# ---------------------------------------------------------------------------
# Auth deshabilitada (solo desarrollo local)
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_disabled_client(session: Session) -> Generator[TestClient, None, None]:
    def _get_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_auth_credentials] = lambda: make_auth_credentials(enabled=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_login_is_not_available_when_auth_is_disabled(auth_disabled_client: TestClient) -> None:
    """Sin auth no hay sesión que emitir: el endpoint no aplica."""
    response = login(auth_disabled_client, TEST_USERNAME, TEST_PASSWORD)
    assert response.status_code == 404


def test_me_works_without_a_token_when_auth_is_disabled(
    auth_disabled_client: TestClient,
) -> None:
    response = auth_disabled_client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "anonymous"
