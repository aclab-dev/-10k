"""Tests unitarios para la señal de open interest (F5 — tarea #50)."""

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
from backend.quant_signals.open_interest import (
    _OI_NEUTRAL_RATIO,
    _OI_SCALE,
    compute_open_interest_signal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(close: float = 50000.0) -> CandleData:
    c = Decimal(str(close))
    low = c * Decimal("0.99")
    high = c * Decimal("1.01")
    return CandleData(open=c, high=high, low=low, close=c, volume=Decimal("1000"), n_candles=10)


def _snapshot(
    open_interest: float | None = None,
    volume: float = 1000.0,
) -> MarketSnapshot:
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
        open_interest=Decimal(str(open_interest)) if open_interest is not None else None,
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
    def test_signal_in_range_high_oi(self) -> None:
        snap = _snapshot(open_interest=3000.0, volume=1000.0)
        assert -1.0 <= compute_open_interest_signal(snap) <= 1.0

    def test_signal_in_range_low_oi(self) -> None:
        snap = _snapshot(open_interest=100.0, volume=1000.0)
        assert -1.0 <= compute_open_interest_signal(snap) <= 1.0

    def test_signal_in_range_extreme_oi(self) -> None:
        snap = _snapshot(open_interest=100000.0, volume=1000.0)
        assert -1.0 <= compute_open_interest_signal(snap) <= 1.0


# ---------------------------------------------------------------------------
# Dirección de la señal
# ---------------------------------------------------------------------------


class TestSignalDirection:
    def test_oi_greater_than_volume_gives_bearish_signal(self) -> None:
        # OI > volumen → mercado sobre-apalancado → señal bajista
        snap = _snapshot(open_interest=2000.0, volume=1000.0)
        assert compute_open_interest_signal(snap) < 0.0

    def test_oi_less_than_volume_gives_bullish_signal(self) -> None:
        # OI < volumen → mercado des-apalancado → señal alcista
        snap = _snapshot(open_interest=500.0, volume=1000.0)
        assert compute_open_interest_signal(snap) > 0.0

    def test_oi_equals_volume_gives_zero_signal(self) -> None:
        snap = _snapshot(open_interest=1000.0, volume=1000.0)
        assert compute_open_interest_signal(snap) == pytest.approx(0.0, abs=1e-9)

    def test_none_oi_gives_zero_signal(self) -> None:
        snap = _snapshot(open_interest=None, volume=1000.0)
        assert compute_open_interest_signal(snap) == 0.0

    def test_zero_volume_gives_zero_signal(self) -> None:
        snap = _snapshot(open_interest=1000.0, volume=0.0)
        assert compute_open_interest_signal(snap) == 0.0

    def test_higher_oi_gives_more_bearish_signal(self) -> None:
        snap_moderate = _snapshot(open_interest=2000.0, volume=1000.0)
        snap_high = _snapshot(open_interest=4000.0, volume=1000.0)
        assert compute_open_interest_signal(snap_high) < compute_open_interest_signal(snap_moderate)

    def test_symmetry_around_neutral(self) -> None:
        # ratio=2.0 → desvío +1.0; ratio=0.0 → desvío -1.0 (señales simétricas)
        snap_high = _snapshot(open_interest=2000.0, volume=1000.0)
        # OI ≈ 0 es ratio → 0.0, desvío = -1.0
        snap_low = _snapshot(open_interest=0.001, volume=1000.0)
        sig_high = compute_open_interest_signal(snap_high)
        sig_low = compute_open_interest_signal(snap_low)
        assert sig_high < 0.0
        assert sig_low > 0.0
        assert abs(sig_high) == pytest.approx(abs(sig_low), rel=1e-3)


# ---------------------------------------------------------------------------
# Detección de extremos (saturación tanh)
# ---------------------------------------------------------------------------


class TestExtremeDetection:
    def test_very_high_oi_saturates_near_minus_one(self) -> None:
        # ratio >> 1 → tanh satura cerca de -1.0
        snap = _snapshot(open_interest=100000.0, volume=1000.0)
        assert compute_open_interest_signal(snap) == pytest.approx(-1.0, abs=0.001)

    def test_very_low_oi_gives_max_bullish_signal(self) -> None:
        # ratio → 0 (OI ≈ 0): signal = -tanh((0-1)/1) = tanh(1) ≈ 0.76.
        # El alcista topa en ~0.76 porque ratio ≥ 0 siempre; no hay saturación a +1.0.
        snap = _snapshot(open_interest=0.001, volume=1000.0)
        signal = compute_open_interest_signal(snap)
        assert signal == pytest.approx(math.tanh(1.0), rel=1e-3)


# ---------------------------------------------------------------------------
# Normalización tanh conocida
# ---------------------------------------------------------------------------


class TestKnownTanhValues:
    def test_ratio_two_gives_minus_tanh_one(self) -> None:
        # OI = 2 × volumen → ratio = 2.0 → signal = -tanh((2-1)/1) = -tanh(1) ≈ -0.7616
        snap = _snapshot(open_interest=2000.0, volume=1000.0)
        assert compute_open_interest_signal(snap) == pytest.approx(-math.tanh(1.0), rel=1e-6)

    def test_ratio_zero_gives_tanh_of_neutral(self) -> None:
        # ratio → 0 → signal = -tanh((0-1)/1) = tanh(1) ≈ 0.7616
        snap = _snapshot(open_interest=0.001, volume=1000.0)
        expected = -math.tanh((0.001 / 1000.0 - _OI_NEUTRAL_RATIO) / _OI_SCALE)
        assert compute_open_interest_signal(snap) == pytest.approx(expected, rel=1e-3)

    def test_ratio_at_neutral_gives_zero(self) -> None:
        # OI = volumen × _OI_NEUTRAL_RATIO → signal = 0
        oi = 1000.0 * _OI_NEUTRAL_RATIO
        snap = _snapshot(open_interest=oi, volume=1000.0)
        assert compute_open_interest_signal(snap) == pytest.approx(0.0, abs=1e-9)
