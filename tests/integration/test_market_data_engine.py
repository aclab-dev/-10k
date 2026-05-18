"""Tests de integración: MarketDataEngine contra PostgreSQL real (F4).

Verifican el flujo validate → persist incluyendo el comportamiento real de
la columna JSONB `extra` y el redondeo de Numeric(20,8).

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.core.config import Environment
from backend.market_data.engine import MarketDataEngine
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
)
from backend.market_data.schemas import (
    MarketSnapshot as MarketSnapshotSchema,
)
from backend.market_data.validators import SnapshotRejectedError
from backend.storage.models import BotRun
from backend.storage.models import MarketSnapshot as MarketSnapshotORM
from backend.storage.repositories.snapshots import MarketSnapshotRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
# MarketSnapshotRepository — save_snapshot contra PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMarketSnapshotRepositoryPostgres:
    def test_save_snapshot_persists_record(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        snap = _valid_snapshot()

        repo = MarketSnapshotRepository(pg_session)
        orm = repo.save_snapshot(snap, run.id)

        assert orm.id == snap.snapshot_id
        assert orm.symbol == "BTCUSDT"
        assert orm.bot_run_id == run.id
        assert orm.close == Decimal("50005")

    def test_save_snapshot_extra_jsonb_roundtrip(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        snap = _valid_snapshot()

        repo = MarketSnapshotRepository(pg_session)
        orm = repo.save_snapshot(snap, run.id)
        pg_session.expire(orm)

        assert orm.extra is not None
        assert "candles" in orm.extra
        assert "tf_5m" in orm.extra["candles"]
        assert orm.extra["data_freshness_status"] == DataFreshnessStatus.FRESH
        assert orm.extra["coherence_status"] == CoherenceStatus.OK
        assert orm.extra["exchange"] == Exchange.BINGX


# ---------------------------------------------------------------------------
# MarketDataEngine
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMarketDataEnginePostgres:
    def test_process_valid_snapshot_returns_orm(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine = MarketDataEngine(pg_session, run.id)
        snap = _valid_snapshot()

        result = engine.process_snapshot(snap)

        assert isinstance(result, MarketSnapshotORM)
        assert result.id == snap.snapshot_id
        assert result.symbol == "BTCUSDT"
        assert result.bot_run_id == run.id

    def test_process_valid_snapshot_is_queryable(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine = MarketDataEngine(pg_session, run.id)
        snap = _valid_snapshot(symbol="ETHUSDT")

        engine.process_snapshot(snap)

        repo = MarketSnapshotRepository(pg_session)
        latest = repo.get_latest_by_symbol(run.id, "ETHUSDT")
        assert latest is not None
        assert latest.id == snap.snapshot_id

    def test_process_expired_snapshot_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine_obj = MarketDataEngine(pg_session, run.id)
        expired_ts = datetime.now(UTC) - timedelta(seconds=35)
        snap = _valid_snapshot(
            timestamp_utc=expired_ts,
            exchange_server_time=expired_ts,
            local_time=expired_ts,
        )

        with pytest.raises(SnapshotRejectedError, match="expirado"):
            engine_obj.process_snapshot(snap)

    def test_process_snapshot_with_invalid_coherence_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine_obj = MarketDataEngine(pg_session, run.id)
        bad_candle = CandleData(
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50010"),
            volume=Decimal("100"),
            n_candles=1,
        )
        snap = _valid_snapshot(
            candles=Candles(
                tf_5m=bad_candle,
                tf_15m=bad_candle,
                tf_1h=bad_candle,
                tf_4h=bad_candle,
            )
        )

        with pytest.raises(SnapshotRejectedError, match="incoherentes"):
            engine_obj.process_snapshot(snap)

    def test_process_rejected_snapshot_is_not_persisted(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine_obj = MarketDataEngine(pg_session, run.id)
        bad_candle = CandleData(
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50010"),
            volume=Decimal("100"),
            n_candles=1,
        )
        snap = _valid_snapshot(
            symbol="SOLUSDT",
            candles=Candles(
                tf_5m=bad_candle,
                tf_15m=bad_candle,
                tf_1h=bad_candle,
                tf_4h=bad_candle,
            ),
        )

        with pytest.raises(SnapshotRejectedError):
            engine_obj.process_snapshot(snap)

        repo = MarketSnapshotRepository(pg_session)
        assert repo.get_latest_by_symbol(run.id, "SOLUSDT") is None

    def test_process_extra_jsonb_roundtrip(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        engine_obj = MarketDataEngine(pg_session, run.id)
        snap = _valid_snapshot()

        result = engine_obj.process_snapshot(snap)
        pg_session.expire(result)

        assert result.extra is not None
        assert "candles" in result.extra
        assert result.extra["data_freshness_status"] == DataFreshnessStatus.FRESH
        assert result.extra["coherence_status"] == CoherenceStatus.OK
