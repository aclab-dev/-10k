"""Tests de los endpoints GET /api/decisions y GET /api/decisions/{id}."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_bot_run, make_decision


def test_list_decisions_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.get("/api/decisions")
    assert response.status_code == 404


def test_list_decisions_empty_when_no_data(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/decisions")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_decisions_pagination(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    for _ in range(5):
        make_decision(session, bot_run)

    response = client.get("/api/decisions", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_list_decisions_filter_by_symbol(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, symbol="BTCUSDT")
    make_decision(session, bot_run, symbol="ETHUSDT")

    response = client.get("/api/decisions", params={"symbol": "ETHUSDT"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["symbol"] == "ETHUSDT"


def test_list_decisions_filter_by_action(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, action="OPEN")
    make_decision(session, bot_run, action="NO_OPERAR")

    response = client.get("/api/decisions", params={"action": "NO_OPERAR"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "NO_OPERAR"


def test_list_decisions_invalid_symbol_rejected(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/decisions", params={"symbol": "DOGEUSDT"})
    assert response.status_code == 422


def test_list_decisions_invalid_action_rejected(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/decisions", params={"action": "HODL"})
    assert response.status_code == 422


def test_get_decision_detail(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    decision = make_decision(session, bot_run)

    response = client.get(f"/api/decisions/{decision.id}")
    assert response.status_code == 200
    assert response.json()["id"] == decision.id


def test_get_decision_404_when_malformed_id(client: TestClient, session: Session) -> None:
    response = client.get("/api/decisions/does-not-exist")
    assert response.status_code == 404


def test_get_decision_404_when_well_formed_but_missing(
    client: TestClient, session: Session
) -> None:
    response = client.get("/api/decisions/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
