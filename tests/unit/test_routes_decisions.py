"""Tests de los endpoints GET /api/decisions y GET /api/decisions/{id}."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.unit.conftest import make_bot_run, make_decision

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)


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


def test_list_decisions_filter_from_ts_is_inclusive(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, timestamp=_T0)
    make_decision(session, bot_run, timestamp=_T1)

    response = client.get("/api/decisions", params={"from_ts": _T1.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["timestamp"].startswith("2026-03-02T12:00:00")


def test_list_decisions_filter_to_ts_is_exclusive(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, timestamp=_T0)
    make_decision(session, bot_run, timestamp=_T1)

    response = client.get("/api/decisions", params={"to_ts": _T1.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["timestamp"].startswith("2026-03-01T12:00:00")


def test_list_decisions_filter_by_range(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, timestamp=_T0)
    make_decision(session, bot_run, timestamp=_T1)
    make_decision(session, bot_run, timestamp=_T2)

    response = client.get(
        "/api/decisions", params={"from_ts": _T1.isoformat(), "to_ts": _T2.isoformat()}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["timestamp"].startswith("2026-03-02T12:00:00")


def test_list_decisions_range_combines_with_symbol_and_action(
    client: TestClient, session: Session
) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, symbol="BTCUSDT", action="OPEN", timestamp=_T1)
    make_decision(session, bot_run, symbol="ETHUSDT", action="OPEN", timestamp=_T1)
    make_decision(session, bot_run, symbol="BTCUSDT", action="NO_OPERAR", timestamp=_T1)
    make_decision(session, bot_run, symbol="BTCUSDT", action="OPEN", timestamp=_T0)

    response = client.get(
        "/api/decisions",
        params={"from_ts": _T1.isoformat(), "symbol": "BTCUSDT", "action": "OPEN"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1


def test_list_decisions_naive_from_ts_is_read_as_utc(client: TestClient, session: Session) -> None:
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, timestamp=_T0)
    make_decision(session, bot_run, timestamp=_T1)

    response = client.get("/api/decisions", params={"from_ts": "2026-03-02T12:00:00"})
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_list_decisions_from_ts_with_offset_is_converted_to_utc(
    client: TestClient, session: Session
) -> None:
    """14:00-02:00 es el mismo instante que 12:00Z: el borde inclusivo debe incluir _T1."""
    bot_run = make_bot_run(session)
    make_decision(session, bot_run, timestamp=_T0)
    make_decision(session, bot_run, timestamp=_T1)

    response = client.get("/api/decisions", params={"from_ts": "2026-03-02T14:00:00+02:00"})
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_list_decisions_inverted_range_rejected(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get(
        "/api/decisions", params={"from_ts": _T2.isoformat(), "to_ts": _T0.isoformat()}
    )
    assert response.status_code == 422


def test_list_decisions_malformed_from_ts_rejected(client: TestClient, session: Session) -> None:
    make_bot_run(session)
    response = client.get("/api/decisions", params={"from_ts": "ayer"})
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
