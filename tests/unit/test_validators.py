"""Tests del validador de frescura y coherencia de snapshots (F4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from backend.market_data.validators import (
    SnapshotRejectedError,
    evaluate_coherence,
    evaluate_freshness,
    validate_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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


def _candles(**tf_overrides: CandleData) -> Candles:
    base: dict[str, CandleData] = {
        "tf_5m": _candle(),
        "tf_15m": _candle(),
        "tf_1h": _candle(),
        "tf_4h": _candle(),
    }
    base.update(tf_overrides)
    return Candles(**base)


def _snapshot(**overrides: object) -> MarketSnapshot:
    defaults: dict[str, object] = {
        "timestamp_utc": _BASE_NOW,
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
        "exchange_server_time": _BASE_NOW,
        "local_time": _BASE_NOW,
        "clock_skew_ms": 10,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshot(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_freshness
# ---------------------------------------------------------------------------


class TestEvaluateFreshness:
    def test_fresh_within_10s(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=5))
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.FRESH

    def test_stale_between_10s_and_30s(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=15))
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.STALE

    def test_expired_beyond_30s(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=35))
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.EXPIRED

    def test_boundary_10s_is_stale(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=11))
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.STALE

    def test_boundary_30s_is_expired(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=31))
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.EXPIRED

    def test_zero_age_is_fresh(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW)
        assert evaluate_freshness(snap, now=_BASE_NOW) == DataFreshnessStatus.FRESH

    def test_now_defaults_to_utc_now(self) -> None:
        snap = _snapshot(timestamp_utc=datetime.now(UTC))
        result = evaluate_freshness(snap)
        assert result == DataFreshnessStatus.FRESH


# ---------------------------------------------------------------------------
# evaluate_coherence — gaps
# ---------------------------------------------------------------------------


class TestEvaluateCoherenceGaps:
    def test_ok_with_sufficient_candles(self) -> None:
        snap = _snapshot()
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.OK
        assert issues == []

    def test_gap_in_5m_is_invalid(self) -> None:
        snap = _snapshot(candles=_candles(tf_5m=_candle(n_candles=1)))
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID
        assert any("5m" in i for i in issues)

    def test_gap_in_15m_is_invalid(self) -> None:
        snap = _snapshot(candles=_candles(tf_15m=_candle(n_candles=2)))
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID
        assert any("15m" in i for i in issues)

    def test_gap_in_1h_is_invalid(self) -> None:
        snap = _snapshot(candles=_candles(tf_1h=_candle(n_candles=1)))
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID
        assert any("1h" in i for i in issues)

    def test_gap_in_4h_is_invalid(self) -> None:
        snap = _snapshot(candles=_candles(tf_4h=_candle(n_candles=2)))
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID
        assert any("4h" in i for i in issues)

    def test_gap_in_multiple_tfs_reports_all(self) -> None:
        snap = _snapshot(
            candles=_candles(
                tf_15m=_candle(n_candles=1),
                tf_4h=_candle(n_candles=2),
            )
        )
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID
        assert len([i for i in issues if "gap" in i]) >= 2

    def test_exactly_min_candles_is_ok(self) -> None:
        snap = _snapshot(candles=_candles(tf_5m=_candle(n_candles=3)))
        status, _ = evaluate_coherence(snap)
        assert status == CoherenceStatus.OK


# ---------------------------------------------------------------------------
# evaluate_coherence — coherencia de precios
# ---------------------------------------------------------------------------


class TestEvaluateCoherencePrices:
    def test_warning_when_last_price_diverges_from_5m_close(self) -> None:
        # last_price=50005, 5m close=48000 → deviation ≈ 4.18% > 2%
        snap = _snapshot(
            candles=_candles(
                tf_5m=_candle(open="48000", high="48500", low="47500", close="48000")
            )
        )
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.WARNING
        assert issues

    def test_ok_when_prices_aligned(self) -> None:
        # default: last_price=50005, 5m close=50010 → deviation ≈ 0.01%
        snap = _snapshot()
        status, _ = evaluate_coherence(snap)
        assert status == CoherenceStatus.OK

    def test_gap_takes_priority_over_price_warning(self) -> None:
        snap = _snapshot(
            candles=_candles(
                tf_5m=_candle(
                    open="48000", high="48500", low="47500", close="48000", n_candles=1
                )
            )
        )
        status, issues = evaluate_coherence(snap)
        assert status == CoherenceStatus.INVALID


# ---------------------------------------------------------------------------
# validate_snapshot — gate
# ---------------------------------------------------------------------------


class TestValidateSnapshot:
    def test_passes_fresh_coherent_snapshot(self) -> None:
        snap = _snapshot()
        result = validate_snapshot(snap, now=_BASE_NOW)
        assert result is snap

    def test_raises_for_expired_snapshot(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=35))
        with pytest.raises(SnapshotRejectedError, match="expirado"):
            validate_snapshot(snap, now=_BASE_NOW)

    def test_raises_for_invalid_coherence(self) -> None:
        snap = _snapshot(candles=_candles(tf_5m=_candle(n_candles=1)))
        with pytest.raises(SnapshotRejectedError, match="incoherentes"):
            validate_snapshot(snap, now=_BASE_NOW)

    def test_stale_snapshot_is_not_rejected(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=15))
        result = validate_snapshot(snap, now=_BASE_NOW)
        assert result is snap

    def test_warning_coherence_is_not_rejected(self) -> None:
        snap = _snapshot(
            candles=_candles(
                tf_5m=_candle(open="48000", high="48500", low="47500", close="48000")
            )
        )
        result = validate_snapshot(snap, now=_BASE_NOW)
        assert result is snap

    def test_error_carries_snapshot_id(self) -> None:
        snap = _snapshot(timestamp_utc=_BASE_NOW - timedelta(seconds=35))
        with pytest.raises(SnapshotRejectedError) as exc_info:
            validate_snapshot(snap, now=_BASE_NOW)
        assert exc_info.value.snapshot_id == snap.snapshot_id

    def test_error_carries_reason(self) -> None:
        snap = _snapshot(candles=_candles(tf_5m=_candle(n_candles=1)))
        with pytest.raises(SnapshotRejectedError) as exc_info:
            validate_snapshot(snap, now=_BASE_NOW)
        assert exc_info.value.reason
