"""Tests de los endpoints GET /api/tokens/usage y GET /api/tokens/budget."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_bot_run, make_token_usage


def test_list_token_usage_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.get("/api/tokens/usage")
    assert response.status_code == 404


def test_list_token_usage_empty_when_no_data(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/tokens/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_token_usage_pagination(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    for _ in range(4):
        make_token_usage(session, bot_run)

    response = client.get("/api/tokens/usage", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2


def test_list_token_usage_filter_by_model(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_token_usage(session, bot_run, model="gpt-5.5")
    make_token_usage(session, bot_run, model="gpt-5.5-mini")

    response = client.get("/api/tokens/usage", params={"model": "gpt-5.5-mini"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["model"] == "gpt-5.5-mini"


def test_token_budget_normal_when_low_usage(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_token_usage(session, bot_run, prompt_tokens=100, completion_tokens=50)

    response = client.get("/api/tokens/budget")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "NORMAL"
    assert body["ok"] is True
    assert body["tokens_used_hour"] == 150
    assert body["limit_hour"] == 100_000


def test_token_budget_exceeded_when_over_hourly_limit(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_token_usage(session, bot_run, prompt_tokens=100_000, completion_tokens=1)

    response = client.get("/api/tokens/budget")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "EXCEEDED"
    assert body["ok"] is False


def test_token_budget_404_when_no_active_bot_run(client: TestClient) -> None:
    response = client.get("/api/tokens/budget")
    assert response.status_code == 404
