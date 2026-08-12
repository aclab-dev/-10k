"""Tests de los endpoints POST /api/auth/login y GET /api/auth/me."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.api import routes_auth
from backend.app.main import app
from backend.auth.config import get_auth_credentials, get_login_throttle_config
from backend.auth.tokens import issue_token
from backend.core.config import LoginThrottleConfig
from backend.storage.database import get_db
from backend.storage.models import LoginLockout
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


# ---------------------------------------------------------------------------
# Rate limiting y lockout (F15)
#
# La lógica del throttle (ventana, backoff, liberación) se testea con el reloj
# inyectado en test_auth_throttle.py. Acá se verifica el contrato HTTP: 429,
# Retry-After y qué se filtra (nada) en la respuesta.
# ---------------------------------------------------------------------------

MAX_FAILURES = 3


def make_throttle_config(**overrides) -> LoginThrottleConfig:
    defaults = {
        "enabled": True,
        "max_failures_per_username": MAX_FAILURES,
        # Todas las requests del TestClient comparten IP: si el límite por IP
        # fuera bajo, se dispararía antes que el que cada test quiere observar.
        "max_failures_per_ip": 50,
        "window_seconds": 600,
        "lockout_seconds": 60,
        "lockout_backoff_factor": 2.0,
        "max_lockout_seconds": 300,
    }
    return LoginThrottleConfig(**{**defaults, **overrides})


@pytest.fixture
def throttled_client(anon_client: TestClient) -> TestClient:
    """Cliente con umbrales chicos, para no gastar decenas de scrypt por test."""
    app.dependency_overrides[get_login_throttle_config] = lambda: make_throttle_config()
    return anon_client


@pytest.fixture
def unthrottled_client(anon_client: TestClient) -> TestClient:
    app.dependency_overrides[get_login_throttle_config] = lambda: make_throttle_config(
        enabled=False
    )
    return anon_client


def fail_login(client: TestClient, username: str = TEST_USERNAME):
    return login(client, username, "password-incorrecta")


def exhaust_quota(client: TestClient, username: str = TEST_USERNAME) -> None:
    """Agota los intentos permitidos. El último fallo aún responde 401, no 429."""
    for _ in range(MAX_FAILURES):
        assert fail_login(client, username).status_code == 401


def test_login_returns_429_once_the_quota_is_exhausted(throttled_client: TestClient) -> None:
    exhaust_quota(throttled_client)
    assert fail_login(throttled_client).status_code == 429


def test_the_429_carries_a_usable_retry_after(throttled_client: TestClient) -> None:
    exhaust_quota(throttled_client)
    response = fail_login(throttled_client)

    retry_after = response.headers["Retry-After"]
    assert retry_after.isdigit()
    assert 0 < int(retry_after) <= 60


def test_the_lockout_also_rejects_the_correct_password(throttled_client: TestClient) -> None:
    """Si no, el atacante que acierta durante el bloqueo entra igual."""
    exhaust_quota(throttled_client)
    assert login(throttled_client, TEST_USERNAME, TEST_PASSWORD).status_code == 429


def test_the_429_does_not_reveal_whether_the_user_exists(throttled_client: TestClient) -> None:
    exhaust_quota(throttled_client, TEST_USERNAME)
    exhaust_quota(throttled_client, "usuario-que-no-existe")

    real = fail_login(throttled_client, TEST_USERNAME)
    fake = fail_login(throttled_client, "usuario-que-no-existe")

    assert real.status_code == fake.status_code == 429
    assert real.json()["detail"] == fake.json()["detail"]


def test_the_429_message_does_not_name_the_scope(throttled_client: TestClient) -> None:
    """Decir si bloqueó el usuario o la IP describe el estado interno del throttle."""
    exhaust_quota(throttled_client)
    detail = fail_login(throttled_client).json()["detail"].lower()
    assert "ip" not in detail.split()
    assert TEST_USERNAME not in detail


def test_a_locked_out_request_never_hashes_the_password(
    throttled_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El punto del chequeo previo: scrypt cuesta ~100 ms y ~64 MB por intento."""
    exhaust_quota(throttled_client)

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("verify_password no debería ejecutarse con la identidad bloqueada")

    monkeypatch.setattr(routes_auth, "verify_password", _boom)
    assert fail_login(throttled_client).status_code == 429


def test_a_successful_login_restores_the_full_quota(throttled_client: TestClient) -> None:
    for _ in range(MAX_FAILURES - 1):
        assert fail_login(throttled_client).status_code == 401
    assert login(throttled_client, TEST_USERNAME, TEST_PASSWORD).status_code == 200

    # Con el contador reseteado vuelven a hacer falta MAX_FAILURES fallos.
    exhaust_quota(throttled_client)
    assert fail_login(throttled_client).status_code == 429


def test_login_works_again_once_the_lockout_expires(
    throttled_client: TestClient, session: Session
) -> None:
    exhaust_quota(throttled_client)
    assert fail_login(throttled_client).status_code == 429

    session.execute(
        update(LoginLockout).values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.commit()

    assert login(throttled_client, TEST_USERNAME, TEST_PASSWORD).status_code == 200


def test_no_lockout_when_the_throttle_is_disabled(unthrottled_client: TestClient) -> None:
    for _ in range(MAX_FAILURES + 3):
        assert fail_login(unthrottled_client).status_code == 401


def test_the_throttle_does_not_change_the_happy_path(throttled_client: TestClient) -> None:
    """El contrato de /api/auth/login para el caso feliz queda igual que en [109]."""
    response = login(throttled_client, TEST_USERNAME, TEST_PASSWORD)
    assert response.status_code == 200
    assert "Retry-After" not in response.headers

    body = response.json()
    assert body["token_type"] == "bearer"
    me = throttled_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
