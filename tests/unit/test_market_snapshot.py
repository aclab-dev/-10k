"""Tests del contrato MarketSnapshot (sección 4.10.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(
    open: str = "50000",
    high: str = "50100",
    low: str = "49900",
    close: str = "50010",
    volume: str = "100",
    n_candles: int = 10,
) -> CandleData:
    return CandleData(
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        n_candles=n_candles,
    )


def _candles() -> Candles:
    return Candles(
        tf_5m=_candle(),
        tf_15m=_candle(),
        tf_1h=_candle(),
        tf_4h=_candle(),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot(**overrides: object) -> MarketSnapshot:
    defaults: dict[str, object] = {
        "timestamp_utc": _now(),
        "exchange": Exchange.BINGX,
        "environment": Environment.PAPER,
        "symbol": "BTCUSDT",
        "last_price": Decimal("50005"),
        "bid": Decimal("50000"),
        "ask": Decimal("50010"),
        "spread_absolute": Decimal("10"),
        "spread_percent": Decimal("0.02"),
        "candles": _candles(),
        "volume": Decimal("1000"),
        "account_balance_usdt": Decimal("500"),
        "open_positions_count": 0,
        "active_orders_count": 0,
        "latency_ms": 50,
        "exchange_server_time": _now(),
        "local_time": _now(),
        "clock_skew_ms": 10,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshot(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CandleData
# ---------------------------------------------------------------------------


class TestCandleData:
    def test_valid_candle(self) -> None:
        c = _candle()
        assert c.close == Decimal("50010")

    def test_low_greater_than_high_raises(self) -> None:
        with pytest.raises(ValueError, match="low"):
            _candle(low="50200", high="50100")

    def test_open_outside_range_raises(self) -> None:
        with pytest.raises(ValueError, match="open"):
            _candle(open="50200", high="50100", low="49900")

    def test_close_outside_range_raises(self) -> None:
        with pytest.raises(ValueError, match="close"):
            _candle(close="49800", high="50100", low="49900")

    def test_negative_volume_raises(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            _candle(volume="-1")

    def test_candle_immutable(self) -> None:
        from pydantic import ValidationError

        c = _candle()
        with pytest.raises(ValidationError):
            c.open = Decimal("99999")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MarketSnapshot — campos básicos
# ---------------------------------------------------------------------------


class TestMarketSnapshotSymbol:
    def test_valid_symbols(self) -> None:
        for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"):
            s = _snapshot(symbol=sym)
            assert s.symbol == sym

    def test_invalid_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="no permitido"):
            _snapshot(symbol="DOGEUSDT")


class TestMarketSnapshotTimestamps:
    def test_naive_timestamp_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _snapshot(timestamp_utc=datetime(2026, 1, 1))

    def test_naive_exchange_server_time_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _snapshot(exchange_server_time=datetime(2026, 1, 1))

    def test_aware_timestamp_accepted(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        s = _snapshot(timestamp_utc=ts)
        assert s.timestamp_utc == ts


# ---------------------------------------------------------------------------
# MarketSnapshot — coherencia de precios
# ---------------------------------------------------------------------------


class TestMarketSnapshotPriceCoherence:
    def test_bid_greater_than_ask_raises(self) -> None:
        with pytest.raises(ValueError, match="bid"):
            _snapshot(bid=Decimal("50020"), ask=Decimal("50010"))

    def test_bid_equal_ask_raises(self) -> None:
        with pytest.raises(ValueError, match="bid"):
            _snapshot(bid=Decimal("50010"), ask=Decimal("50010"))

    def test_last_price_below_bid_raises(self) -> None:
        with pytest.raises(ValueError, match="last_price"):
            _snapshot(
                bid=Decimal("50000"),
                ask=Decimal("50010"),
                spread_absolute=Decimal("10"),
                last_price=Decimal("49999"),
            )

    def test_last_price_above_ask_raises(self) -> None:
        with pytest.raises(ValueError, match="last_price"):
            _snapshot(
                bid=Decimal("50000"),
                ask=Decimal("50010"),
                spread_absolute=Decimal("10"),
                last_price=Decimal("50011"),
            )

    def test_spread_inconsistent_raises(self) -> None:
        with pytest.raises(ValueError, match="spread_absolute"):
            _snapshot(
                bid=Decimal("50000"),
                ask=Decimal("50010"),
                spread_absolute=Decimal("5"),
                last_price=Decimal("50005"),
            )

    def test_excessive_spread_percent_raises(self) -> None:
        with pytest.raises(ValueError, match="spread_percent"):
            _snapshot(spread_percent=Decimal("10.0"))


# ---------------------------------------------------------------------------
# MarketSnapshot — latencia y clock skew
# ---------------------------------------------------------------------------


class TestMarketSnapshotLatency:
    def test_excessive_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency_ms"):
            _snapshot(latency_ms=15_000)

    def test_excessive_clock_skew_raises(self) -> None:
        with pytest.raises(ValueError, match="clock_skew_ms"):
            _snapshot(clock_skew_ms=6_000)

    def test_negative_clock_skew_excessive_raises(self) -> None:
        with pytest.raises(ValueError, match="clock_skew_ms"):
            _snapshot(clock_skew_ms=-6_000)

    def test_negative_clock_skew_within_limit_accepted(self) -> None:
        s = _snapshot(clock_skew_ms=-100)
        assert s.clock_skew_ms == -100


# ---------------------------------------------------------------------------
# MarketSnapshot — snapshot_id y inmutabilidad
# ---------------------------------------------------------------------------


class TestMarketSnapshotMeta:
    def test_snapshot_id_generated(self) -> None:
        s = _snapshot()
        assert len(s.snapshot_id) == 36

    def test_snapshot_id_unique(self) -> None:
        s1 = _snapshot()
        s2 = _snapshot()
        assert s1.snapshot_id != s2.snapshot_id

    def test_snapshot_immutable(self) -> None:
        from pydantic import ValidationError

        s = _snapshot()
        with pytest.raises(ValidationError):
            s.last_price = Decimal("99999")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_returns_required_keys(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid-123")
        required = {
            "id",
            "bot_run_id",
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "bid",
            "ask",
            "spread",
            "extra",
        }
        assert required.issubset(kwargs.keys())

    def test_id_matches_snapshot_id(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid")
        assert kwargs["id"] == s.snapshot_id

    def test_close_is_last_price(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid")
        assert kwargs["close"] == s.last_price

    def test_extra_contains_candles(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid")
        assert "candles" in kwargs["extra"]
        assert "tf_5m" in kwargs["extra"]["candles"]

    def test_extra_contains_status_fields(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid")
        extra = kwargs["extra"]
        assert extra["data_freshness_status"] == DataFreshnessStatus.FRESH
        assert extra["coherence_status"] == CoherenceStatus.OK

    def test_extra_contains_exchange_environment(self) -> None:
        s = _snapshot()
        kwargs = s.to_db_kwargs(bot_run_id="run-uuid")
        extra = kwargs["extra"]
        assert extra["exchange"] == Exchange.BINGX
        assert extra["environment"] == Environment.PAPER
