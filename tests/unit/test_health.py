"""Tests del endpoint /health del servicio app."""

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.core.config import APP_VERSION, Environment, get_settings


def test_health_returns_200() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200


def test_health_includes_version_and_mode() -> None:
    client = TestClient(app)
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
    assert body["mode"] in {e.value for e in Environment}


def test_health_mode_reflects_settings() -> None:
    """El campo mode debe coincidir con el ENVIRONMENT configurado."""
    settings = get_settings()
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["mode"] == settings.environment.value
