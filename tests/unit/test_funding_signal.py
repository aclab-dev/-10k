"""Tests unitarios para la señal de funding rate (F5 — tarea #50)."""

from __future__ import annotations

import math
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
from backend.quant_signals.funding import _FUNDING_SCALE, compute_funding_signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(close: float = 50000.0) -> CandleData:
    c = Decimal(str(close))
    low = c * Decimal("0.99")
    high = c * Decimal("1.01")
    return CandleData(open=c, high=high, low=low, close=c, volume=Decimal("1000"), n_candles=10)


def _snapshot(funding_rate: float | None = None, volume: float = 1000.0) -> MarketSnapshot:
    lp = Decimal("50000")
    bid = lp * Decimal("0.9999")
    ask = lp * Decimal("1.0001")
    spread_abs = ask - bid
    spread_pct = spread_abs / bid * 100
    now = datetime.now(UTC)
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.BINGX,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=lp,
        bid=bid,
        ask=ask,
        spread_absolute=spread_abs,
        spread_percent=spread_pct,
        candles=Candles(
            tf_5m=_candle(),
            tf_15m=_candle(),
            tf_1h=_candle(),
            tf_4h=_candle(),
        ),
        volume=Decimal(str(volume)),
        funding_rate=funding_rate,
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=now,
        local_time=now,
        clock_skew_ms=10,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


# ---------------------------------------------------------------------------
# Rango de salida
# ---------------------------------------------------------------------------


class TestOutputRange:
    def test_signal_in_range_positive_funding(self) -> None:
        snap = _snapshot(funding_rate=0.0001)
        assert -1.0 <= compute_funding_signal(snap) <= 1.0

    def test_signal_in_range_negative_funding(self) -> None:
        snap = _snapshot(funding_rate=-0.0001)
        assert -1.0 <= compute_funding_signal(snap) <= 1.0

    def test_signal_in_range_extreme_positive(self) -> None:
        snap = _snapshot(funding_rate=0.01)
        assert -1.0 <= compute_funding_signal(snap) <= 1.0

    def test_signal_in_range_extreme_negative(self) -> None:
        snap = _snapshot(funding_rate=-0.01)
        assert -1.0 <= compute_funding_signal(snap) <= 1.0


# ---------------------------------------------------------------------------
# Dirección de la señal
# ---------------------------------------------------------------------------


class TestSignalDirection:
    def test_positive_funding_gives_bearish_signal(self) -> None:
        # Longs pagan a shorts → mercado sobre-comprado → señal bajista
        snap = _snapshot(funding_rate=0.0001)
        assert compute_funding_signal(snap) < 0.0

    def test_negative_funding_gives_bullish_signal(self) -> None:
        # Shorts pagan a longs → mercado sobre-vendido → señal alcista
        snap = _snapshot(funding_rate=-0.0001)
        assert compute_funding_signal(snap) > 0.0

    def test_zero_funding_gives_zero_signal(self) -> None:
        snap = _snapshot(funding_rate=0.0)
        assert compute_funding_signal(snap) == 0.0

    def test_none_funding_gives_zero_signal(self) -> None:
        snap = _snapshot(funding_rate=None)
        assert compute_funding_signal(snap) == 0.0

    def test_stronger_funding_gives_stronger_signal(self) -> None:
        snap_moderate = _snapshot(funding_rate=0.0003)
        snap_strong = _snapshot(funding_rate=0.001)
        # Ambas bajistas; la más intensa debe tener mayor magnitud
        assert compute_funding_signal(snap_strong) < compute_funding_signal(snap_moderate)

    def test_symmetry_positive_negative(self) -> None:
        snap_pos = _snapshot(funding_rate=0.0005)
        snap_neg = _snapshot(funding_rate=-0.0005)
        assert compute_funding_signal(snap_pos) == pytest.approx(
            -compute_funding_signal(snap_neg), rel=1e-9
        )


# ---------------------------------------------------------------------------
# Detección de extremos (saturación tanh)
# ---------------------------------------------------------------------------


class TestExtremeDetection:
    def test_very_high_positive_funding_saturates_near_minus_one(self) -> None:
        snap = _snapshot(funding_rate=0.05)
        assert compute_funding_signal(snap) == pytest.approx(-1.0, abs=0.001)

    def test_very_high_negative_funding_saturates_near_plus_one(self) -> None:
        snap = _snapshot(funding_rate=-0.05)
        assert compute_funding_signal(snap) == pytest.approx(1.0, abs=0.001)

    def test_typical_extreme_threshold_gives_strong_signal(self) -> None:
        # Funding de 0.03% (3 × _FUNDING_SCALE) → tanh(3) ≈ 0.995
        snap = _snapshot(funding_rate=3 * _FUNDING_SCALE)
        signal = compute_funding_signal(snap)
        assert abs(signal) > 0.99


# ---------------------------------------------------------------------------
# Normalización tanh conocida
# ---------------------------------------------------------------------------


class TestKnownTanhValues:
    def test_funding_at_scale_gives_tanh_one(self) -> None:
        # rate = _FUNDING_SCALE → tanh(1) ≈ 0.7616 → signal ≈ -0.7616
        snap = _snapshot(funding_rate=_FUNDING_SCALE)
        signal = compute_funding_signal(snap)
        assert signal == pytest.approx(-math.tanh(1.0), rel=1e-6)

    def test_funding_at_half_scale_gives_tanh_half(self) -> None:
        snap = _snapshot(funding_rate=_FUNDING_SCALE / 2)
        signal = compute_funding_signal(snap)
        assert signal == pytest.approx(-math.tanh(0.5), rel=1e-6)
