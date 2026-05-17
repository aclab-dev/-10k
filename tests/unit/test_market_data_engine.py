"""Tests del MarketDataEngine — validate + persist flow (F4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core.config import Environment
from backend.market_data.engine import MarketDataEngine
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot as MarketSnapshotSchema,
)
from backend.market_data.validators import SnapshotRejectedError
from backend.storage.database import Base
from backend.storage.models import BotRun, MarketSnapshot as MarketSnapshotORM
from backend.storage.repositories.snapshots import MarketSnapshotRepository

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


def _valid_candle() -> CandleData:
    return CandleData(
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50010"),
        volume=Decimal("100"),
        n_candles=10,
    )


def _valid_snapshot(**overrides: object) -> MarketSnapshotSchema:
    now = datetime.now(UTC)
    candle = _valid_candle()
    defaults: dict[str, object] = {
        "timestamp_utc": now,
        "exchange": Exchange.BINGX,
        "environment": Environment.PAPER,
        "symbol": "BTCUSDT",
        "last_price": Decimal("50005"),
        "bid": Decimal("50000"),
        "ask": Decimal("50010"),
        "spread_absolute": Decimal("10"),
        "spread_percent": Decimal("0.02"),
        "candles": Candles(tf_5m=candle, tf_15m=candle, tf_1h=candle, tf_4h=candle),
        "volume": Decimal("1000"),
        "account_balance_usdt": Decimal("500"),
        "open_positions_count": 0,
        "active_orders_count": 0,
        "latency_ms": 50,
        "exchange_server_time": now,
        "local_time": now,
        "clock_skew_ms": 10,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshotSchema(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMarketDataEngine:
    def test_process_valid_snapshot_returns_orm(self, session: Session) -> None:
        run = _bot_run(session)
        engine = MarketDataEngine(session, run.id)
        snap = _valid_snapshot()

        result = engine.process_snapshot(snap)

        assert isinstance(result, MarketSnapshotORM)
        assert result.id == snap.snapshot_id
        assert result.symbol == "BTCUSDT"
        assert result.bot_run_id == run.id

    def test_process_valid_snapshot_is_queryable(self, session: Session) -> None:
        run = _bot_run(session)
        engine = MarketDataEngine(session, run.id)
        snap = _valid_snapshot(symbol="ETHUSDT")

        engine.process_snapshot(snap)

        repo = MarketSnapshotRepository(session)
        latest = repo.get_latest_by_symbol(run.id, "ETHUSDT")
        assert latest is not None
        assert latest.id == snap.snapshot_id

    def test_process_snapshot_with_invalid_coherence_raises(self, session: Session) -> None:
        run = _bot_run(session)
        engine_obj = MarketDataEngine(session, run.id)
        bad_candle = CandleData(
            open=Decimal("50000"), high=Decimal("50100"),
            low=Decimal("49900"), close=Decimal("50010"),
            volume=Decimal("100"), n_candles=1,  # gap → INVALID
        )
        snap = _valid_snapshot(
            candles=Candles(
                tf_5m=bad_candle, tf_15m=bad_candle,
                tf_1h=bad_candle, tf_4h=bad_candle,
            )
        )

        with pytest.raises(SnapshotRejectedError):
            engine_obj.process_snapshot(snap)

    def test_process_rejected_snapshot_is_not_persisted(self, session: Session) -> None:
        run = _bot_run(session)
        engine_obj = MarketDataEngine(session, run.id)
        bad_candle = CandleData(
            open=Decimal("50000"), high=Decimal("50100"),
            low=Decimal("49900"), close=Decimal("50010"),
            volume=Decimal("100"), n_candles=1,
        )
        snap = _valid_snapshot(
            symbol="SOLUSDT",
            candles=Candles(
                tf_5m=bad_candle, tf_15m=bad_candle,
                tf_1h=bad_candle, tf_4h=bad_candle,
            ),
        )

        with pytest.raises(SnapshotRejectedError):
            engine_obj.process_snapshot(snap)

        repo = MarketSnapshotRepository(session)
        assert repo.get_latest_by_symbol(run.id, "SOLUSDT") is None

    def test_process_extra_contains_candles_and_status(self, session: Session) -> None:
        run = _bot_run(session)
        engine_obj = MarketDataEngine(session, run.id)
        snap = _valid_snapshot()

        result = engine_obj.process_snapshot(snap)

        assert result.extra is not None
        assert "candles" in result.extra
        assert result.extra["data_freshness_status"] == DataFreshnessStatus.FRESH
        assert result.extra["coherence_status"] == CoherenceStatus.OK
