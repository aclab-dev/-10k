"""Tests de los endpoints GET /api/status y GET /api/status/history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_account_state, make_bot_run, make_bot_state


def test_status_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 404


def test_status_returns_bot_run_state_and_account(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_bot_state(session, bot_run, state="RUNNING_CYCLE", previous_state="IDLE")
    make_account_state(session, bot_run, balance_usdt=Decimal("950"), drawdown_percent=5.0)

    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["bot_run_id"] == bot_run.id
    assert body["environment"] == "PAPER"
    assert body["run_status"] == "RUNNING"
    assert body["state"] == "RUNNING_CYCLE"
    assert body["previous_state"] == "IDLE"
    assert Decimal(body["account"]["balance_usdt"]) == Decimal("950")
    assert body["account"]["drawdown_percent"] == 5.0


def test_status_account_none_without_account_state(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["account"] is None
    assert response.json()["state"] is None


def test_status_explicit_bot_run_id_malformed_returns_404(
    client: TestClient, session: Session
) -> None:
    make_bot_run(session)
    response = client.get("/api/status", params={"bot_run_id": "does-not-exist"})
    assert response.status_code == 404


def test_status_explicit_bot_run_id_well_formed_but_missing_returns_404(
    client: TestClient, session: Session
) -> None:
    make_bot_run(session)
    response = client.get(
        "/api/status", params={"bot_run_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert response.status_code == 404


def test_status_history_paginated(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    base = datetime.now(UTC)
    for i in range(5):
        make_account_state(session, bot_run, timestamp=base + timedelta(minutes=i))

    response = client.get("/api/status/history", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2
    # Orden ASC por timestamp: offset=1 salta el primero.
    # SQLite no preserva tzinfo en el round-trip (a diferencia de TIMESTAMPTZ en
    # Postgres), por eso comparamos naive.
    expected = (base + timedelta(minutes=1)).replace(tzinfo=None)
    assert datetime.fromisoformat(body["items"][0]["timestamp"]) == expected


def test_status_history_filters_since_until(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    base = datetime.now(UTC)
    for i in range(5):
        make_account_state(session, bot_run, timestamp=base + timedelta(hours=i))

    since = (base + timedelta(hours=1)).isoformat()
    until = (base + timedelta(hours=3)).isoformat()
    response = client.get("/api/status/history", params={"since": since, "until": until})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3


def test_status_history_empty_when_no_data(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/status/history")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
