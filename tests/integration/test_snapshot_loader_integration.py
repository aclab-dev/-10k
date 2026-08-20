"""Tests de integración para SnapshotLoader (F11) — requieren PostgreSQL real.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.replay.schemas import SnapshotWindow
from backend.replay.snapshot_loader import SnapshotLoader
from backend.storage.models import BotRun, MarketSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bot_run(session: Session, status: str = "RUNNING") -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status=status,
    )
    session.add(run)
    session.flush()
    return run


def _snapshot(
    session: Session,
    bot_run_id: str,
    symbol: str,
    timestamp: datetime,
    close: Decimal = Decimal("50000"),
) -> MarketSnapshot:
    snap = MarketSnapshot(
        id=str(uuid.uuid4()),
        bot_run_id=bot_run_id,
        symbol=symbol,
        timestamp=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1.0"),
    )
    session.add(snap)
    session.flush()
    return snap


# ---------------------------------------------------------------------------
# SnapshotLoader — tests contra PostgreSQL real
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSnapshotLoaderIntegration:
    def test_load_returns_snapshots_in_range(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            _snapshot(pg_session, run.id, "BTCUSDT", base + timedelta(hours=i))

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=base,
            period_end=base + timedelta(hours=5),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)

        assert len(result) == 5

    def test_load_ordered_asc(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        base = datetime(2026, 2, 1, tzinfo=UTC)
        for i in [4, 2, 0, 3, 1]:
            _snapshot(pg_session, run.id, "ETHUSDT", base + timedelta(hours=i))

        window = SnapshotWindow(
            symbol="ETHUSDT",
            period_start=base,
            period_end=base + timedelta(hours=5),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)

        timestamps = [r.timestamp for r in result]
        assert timestamps == sorted(timestamps)

    def test_load_includes_period_start(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        base = datetime(2026, 3, 1, tzinfo=UTC)
        _snapshot(pg_session, run.id, "SOLUSDT", base)
        _snapshot(pg_session, run.id, "SOLUSDT", base + timedelta(hours=1))

        window = SnapshotWindow(
            symbol="SOLUSDT",
            period_start=base,
            period_end=base + timedelta(hours=2),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)

        assert len(result) == 2
        assert result[0].timestamp == base

    def test_load_excludes_period_end(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        base = datetime(2026, 4, 1, tzinfo=UTC)
        _snapshot(pg_session, run.id, "BNBUSDT", base)
        _snapshot(pg_session, run.id, "BNBUSDT", base + timedelta(hours=1))
        # Exactly at period_end — should be excluded
        _snapshot(pg_session, run.id, "BNBUSDT", base + timedelta(hours=2))

        window = SnapshotWindow(
            symbol="BNBUSDT",
            period_start=base,
            period_end=base + timedelta(hours=2),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)

        assert len(result) == 2

    def test_load_filters_by_symbol(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        base = datetime(2026, 5, 1, tzinfo=UTC)
        _snapshot(pg_session, run.id, "BTCUSDT", base)
        _snapshot(pg_session, run.id, "XRPUSDT", base)

        window = SnapshotWindow(
            symbol="XRPUSDT",
            period_start=base,
            period_end=base + timedelta(hours=1),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)

        assert all(r.symbol == "XRPUSDT" for r in result)
        assert len(result) == 1

    def test_load_with_bot_run_id_filter(self, pg_session: Session) -> None:
        run_a = _bot_run(pg_session)
        run_b = _bot_run(pg_session, status="STOPPED")
        base = datetime(2026, 6, 1, tzinfo=UTC)
        _snapshot(pg_session, run_a.id, "BTCUSDT", base)
        _snapshot(pg_session, run_b.id, "BTCUSDT", base + timedelta(minutes=1))

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=base,
            period_end=base + timedelta(hours=1),
        )
        loader = SnapshotLoader(pg_session)

        all_results = loader.load(window)
        assert len(all_results) == 2

        filtered = loader.load(window, bot_run_id=run_a.id)
        assert len(filtered) == 1
        assert filtered[0].bot_run_id == run_a.id

    def test_load_empty_range_returns_empty(self, pg_session: Session) -> None:
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2099, 1, 1, tzinfo=UTC),
            period_end=datetime(2099, 1, 2, tzinfo=UTC),
        )
        loader = SnapshotLoader(pg_session)
        result = loader.load(window)
        assert result == []
