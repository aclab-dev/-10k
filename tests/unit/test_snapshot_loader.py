"""Tests unitarios para SnapshotLoader (F11)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.replay.schemas import SnapshotWindow
from backend.replay.snapshot_loader import SnapshotLoader
from backend.storage.database import Base
from backend.storage.models import BotRun, MarketSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _bot_run(session: Session) -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status="RUNNING",
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
# SnapshotWindow validation
# ---------------------------------------------------------------------------


class TestSnapshotWindow:
    def test_valid_window(self) -> None:
        w = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert w.symbol == "BTCUSDT"

    def test_invalid_symbol(self) -> None:
        with pytest.raises(ValueError, match="no permitido"):
            SnapshotWindow(
                symbol="DOGEUSDT",
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError, match="posterior"):
            SnapshotWindow(
                symbol="BTCUSDT",
                period_start=datetime(2026, 1, 2, tzinfo=UTC),
                period_end=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_end_equal_start_raises(self) -> None:
        t = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="posterior"):
            SnapshotWindow(symbol="ETHUSDT", period_start=t, period_end=t)


# ---------------------------------------------------------------------------
# SnapshotLoader
# ---------------------------------------------------------------------------


class TestSnapshotLoader:
    def test_load_returns_snapshots_in_range(self, session: Session) -> None:
        run = _bot_run(session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            _snapshot(session, run.id, "BTCUSDT", base + timedelta(hours=i))

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=base,
            period_end=base + timedelta(hours=5),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)

        assert len(result) == 5

    def test_load_ordered_asc(self, session: Session) -> None:
        run = _bot_run(session)
        base = datetime(2026, 2, 1, tzinfo=UTC)
        # Insert in reverse order to verify sorting
        for i in [4, 2, 0, 3, 1]:
            _snapshot(session, run.id, "ETHUSDT", base + timedelta(hours=i))

        window = SnapshotWindow(
            symbol="ETHUSDT",
            period_start=base,
            period_end=base + timedelta(hours=5),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)

        timestamps = [r.timestamp for r in result]
        assert timestamps == sorted(timestamps)

    def test_load_excludes_period_end(self, session: Session) -> None:
        run = _bot_run(session)
        base = datetime(2026, 3, 1, tzinfo=UTC)
        _snapshot(session, run.id, "SOLUSDT", base)
        _snapshot(session, run.id, "SOLUSDT", base + timedelta(hours=1))
        # Exactly at period_end — should be excluded
        _snapshot(session, run.id, "SOLUSDT", base + timedelta(hours=2))

        window = SnapshotWindow(
            symbol="SOLUSDT",
            period_start=base,
            period_end=base + timedelta(hours=2),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)

        assert len(result) == 2

    def test_load_filters_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        base = datetime(2026, 4, 1, tzinfo=UTC)
        _snapshot(session, run.id, "BTCUSDT", base)
        _snapshot(session, run.id, "BNBUSDT", base)

        window = SnapshotWindow(
            symbol="BNBUSDT",
            period_start=base,
            period_end=base + timedelta(hours=1),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)

        assert all(r.symbol == "BNBUSDT" for r in result)
        assert len(result) == 1

    def test_load_with_bot_run_id_filter(self, session: Session) -> None:
        run_a = _bot_run(session)
        run_b = _bot_run(session)
        base = datetime(2026, 5, 1, tzinfo=UTC)
        _snapshot(session, run_a.id, "XRPUSDT", base)
        _snapshot(session, run_b.id, "XRPUSDT", base + timedelta(minutes=1))

        window = SnapshotWindow(
            symbol="XRPUSDT",
            period_start=base,
            period_end=base + timedelta(hours=1),
        )
        loader = SnapshotLoader(session)

        # Without filter: returns both
        all_results = loader.load(window)
        assert len(all_results) == 2

        # With filter: returns only run_a's
        filtered = loader.load(window, bot_run_id=run_a.id)
        assert len(filtered) == 1
        assert filtered[0].bot_run_id == run_a.id

    def test_load_includes_period_start(self, session: Session) -> None:
        run = _bot_run(session)
        base = datetime(2026, 6, 1, tzinfo=UTC)
        # Exactly at period_start — should be included
        _snapshot(session, run.id, "BTCUSDT", base)
        _snapshot(session, run.id, "BTCUSDT", base + timedelta(hours=1))

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=base,
            period_end=base + timedelta(hours=2),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)

        assert len(result) == 2
        assert result[0].timestamp.replace(tzinfo=None) == base.replace(tzinfo=None)

    def test_load_empty_range_returns_empty(self, session: Session) -> None:
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2099, 1, 1, tzinfo=UTC),
            period_end=datetime(2099, 1, 2, tzinfo=UTC),
        )
        loader = SnapshotLoader(session)
        result = loader.load(window)
        assert result == []
