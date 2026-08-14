"""Tests de los bordes de paginación (`limit`/`offset`) compartidos por PageParams.

backend/api/pagination.py define límites (`limit` entre 1 y 200, `offset` >= 0)
consumidos por todos los listados paginados del dashboard. Se prueban una vez acá
sobre un endpoint representativo por status code, y se repiten en los cuatro
endpoints paginados para el caso feliz (defaults + eco de limit/offset).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_bot_run

_PAGINATED_ENDPOINTS = [
    "/api/status/history",
    "/api/decisions",
    "/api/risk/validations",
    "/api/tokens/usage",
]


@pytest.mark.parametrize("path", _PAGINATED_ENDPOINTS)
def test_limit_zero_rejected(client: TestClient, session: Session, path: str) -> None:
    make_bot_run(session)
    response = client.get(path, params={"limit": 0})
    assert response.status_code == 422


@pytest.mark.parametrize("path", _PAGINATED_ENDPOINTS)
def test_limit_over_max_rejected(client: TestClient, session: Session, path: str) -> None:
    make_bot_run(session)
    response = client.get(path, params={"limit": 201})
    assert response.status_code == 422


@pytest.mark.parametrize("path", _PAGINATED_ENDPOINTS)
def test_offset_negative_rejected(client: TestClient, session: Session, path: str) -> None:
    make_bot_run(session)
    response = client.get(path, params={"offset": -1})
    assert response.status_code == 422


@pytest.mark.parametrize("path", _PAGINATED_ENDPOINTS)
def test_limit_at_max_accepted(client: TestClient, session: Session, path: str) -> None:
    make_bot_run(session)
    response = client.get(path, params={"limit": 200})
    assert response.status_code == 200
    assert response.json()["limit"] == 200


@pytest.mark.parametrize("path", _PAGINATED_ENDPOINTS)
def test_default_limit_and_offset(client: TestClient, session: Session, path: str) -> None:
    make_bot_run(session)
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["items"] == []
    assert body["total"] == 0
