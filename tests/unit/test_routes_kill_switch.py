"""Tests del endpoint POST /api/kill-switch."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.models import BotState, KillSwitchEvent
from tests.unit.conftest import make_bot_run, make_bot_state


def test_kill_switch_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 404


def test_kill_switch_401_without_credentials(anon_client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = anon_client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 401


def test_kill_switch_requires_reason(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.post("/api/kill-switch", json={"reason": ""})
    assert response.status_code == 422


def test_kill_switch_from_default_active_state(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    reason = "operador detectó comportamiento anómalo"
    response = client.post("/api/kill-switch", json={"reason": reason})
    assert response.status_code == 200
    body = response.json()
    assert body["bot_run_id"] == bot_run.id
    assert body["state"] == "KILL_SWITCH_TRIGGERED"
    assert body["previous_state"] == "ACTIVE"
    assert body["reason"] == reason

    stored_state = session.scalars(select(BotState).where(BotState.bot_run_id == bot_run.id)).all()
    assert len(stored_state) == 1
    assert stored_state[0].state == "KILL_SWITCH_TRIGGERED"

    events = session.scalars(
        select(KillSwitchEvent).where(KillSwitchEvent.bot_run_id == bot_run.id)
    ).all()
    assert len(events) == 1
    assert events[0].action_taken == "MANUAL_KILL_SWITCH"
    assert events[0].requires_manual_review is True


def test_kill_switch_from_safe_mode(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_bot_state(session, bot_run, state="SAFE_MODE")
    response = client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 200
    assert response.json()["previous_state"] == "SAFE_MODE"


def test_kill_switch_conflict_when_already_triggered(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_bot_state(session, bot_run, state="KILL_SWITCH_TRIGGERED")
    response = client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 409


def test_kill_switch_conflict_from_manual_paused(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_bot_state(session, bot_run, state="MANUAL_PAUSED")
    response = client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 409


def test_kill_switch_conflict_on_stopped_bot_run(client: TestClient, session: Session) -> None:
    """bot_run_id es query param y no filtra por status.

    Un run cerrado no debe aceptar un kill switch nuevo.
    """
    bot_run = make_bot_run(session, status="STOPPED")
    response = client.post(
        "/api/kill-switch", params={"bot_run_id": bot_run.id}, json={"reason": "test"}
    )
    assert response.status_code == 409

    stored_state = session.scalars(select(BotState).where(BotState.bot_run_id == bot_run.id)).all()
    assert len(stored_state) == 0

    events = session.scalars(
        select(KillSwitchEvent).where(KillSwitchEvent.bot_run_id == bot_run.id)
    ).all()
    assert len(events) == 0


def test_kill_switch_rejects_unknown_persisted_state(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_bot_state(session, bot_run, state="IDLE")
    response = client.post("/api/kill-switch", json={"reason": "test"})
    assert response.status_code == 500

    stored_state = session.scalars(select(BotState).where(BotState.bot_run_id == bot_run.id)).all()
    assert len(stored_state) == 1
    assert stored_state[0].state == "IDLE"

    events = session.scalars(
        select(KillSwitchEvent).where(KillSwitchEvent.bot_run_id == bot_run.id)
    ).all()
    assert len(events) == 0
