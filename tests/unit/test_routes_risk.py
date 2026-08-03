"""Tests del endpoint GET /api/risk/validations (incluye la vista de bloqueos)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_bot_run, make_risk_validation


def test_list_risk_validations_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.get("/api/risk/validations")
    assert response.status_code == 404


def test_list_risk_validations_empty_when_no_data(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/risk/validations")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_risk_validations_pagination(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    for _ in range(3):
        make_risk_validation(session, bot_run, result="APPROVE")

    response = client.get("/api/risk/validations", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_list_risk_validations_filter_blocks(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_risk_validation(session, bot_run, result="APPROVE")
    make_risk_validation(session, bot_run, result="BLOCK")
    make_risk_validation(session, bot_run, result="BLOCK")

    response = client.get("/api/risk/validations", params={"result": "BLOCK"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(item["result"] == "BLOCK" for item in body["items"])
    assert body["items"][0]["reasons"] == {"rule": "max_margin_exceeded"}


def test_list_risk_validations_filter_by_symbol(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_risk_validation(session, bot_run, symbol="BTCUSDT", result="BLOCK")
    make_risk_validation(session, bot_run, symbol="ETHUSDT", result="BLOCK")

    response = client.get("/api/risk/validations", params={"symbol": "ETHUSDT"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["symbol"] == "ETHUSDT"


def test_list_risk_validations_filter_no_operar(client: TestClient, session: Session) -> None:
    """NO_OPERAR es un resultado válido: decisión del Aggregator sin edge, propagada
    por el Risk Engine sin evaluarla (no debe confundirse con BLOCK)."""
    bot_run = make_bot_run(session)
    make_risk_validation(session, bot_run, result="NO_OPERAR")
    make_risk_validation(session, bot_run, result="BLOCK")

    response = client.get("/api/risk/validations", params={"result": "NO_OPERAR"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["result"] == "NO_OPERAR"


def test_list_risk_validations_invalid_result_rejected(
    client: TestClient, session: Session
) -> None:
    make_bot_run(session)
    response = client.get("/api/risk/validations", params={"result": "MAYBE"})
    assert response.status_code == 422
