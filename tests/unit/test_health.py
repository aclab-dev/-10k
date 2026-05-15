"""Tests del endpoint /health del servicio app."""

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.core.config import APP_VERSION, Environment, get_settings
from backend.storage.database import get_db


def _db_ok() -> Generator[Session, None, None]:
    mock = MagicMock(spec=Session)
    yield mock


def _db_down() -> Generator[Session, None, None]:
    mock = MagicMock(spec=Session)
    mock.execute.side_effect = OperationalError("DB down", None, None)
    yield mock


@pytest.fixture(autouse=True)
def _clear_overrides() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.clear()


def test_health_returns_200_when_db_ok() -> None:
    app.dependency_overrides[get_db] = _db_ok
    response = TestClient(app).get("/health")
    assert response.status_code == 200


def test_health_includes_version_and_mode() -> None:
    app.dependency_overrides[get_db] = _db_ok
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
    assert body["mode"] in {e.value for e in Environment}


def test_health_mode_reflects_settings() -> None:
    settings = get_settings()
    app.dependency_overrides[get_db] = _db_ok
    body = TestClient(app).get("/health").json()
    assert body["mode"] == settings.environment.value


def test_health_returns_503_when_db_unreachable() -> None:
    app.dependency_overrides[get_db] = _db_down
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


def test_health_503_still_includes_version_and_mode() -> None:
    app.dependency_overrides[get_db] = _db_down
    body = TestClient(app).get("/health").json()
    assert body["version"] == APP_VERSION
    assert body["mode"] in {e.value for e in Environment}
