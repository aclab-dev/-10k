"""Tests unitarios para la señal de mean reversion (F5 — tarea #48)."""

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
from backend.quant_signals.mean_reversion import compute_mean_reversion_signal

_MIN_CV = 1e-6  # valor del guard de mercado plano — hardcodeado para no acoplar al símbolo privado

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(close: float) -> CandleData:
    c = Decimal(str(close))
    low = c * Decimal("0.99")
    high = c * Decimal("1.01")
    return CandleData(open=c, high=high, low=low, close=c, volume=Decimal("1000"), n_candles=10)


def _snapshot(
    close_5m: float,
    close_15m: float,
    close_1h: float,
    close_4h: float,
    last_price: float | None = None,
) -> MarketSnapshot:
    if last_price is None:
        last_price = close_5m
    lp = Decimal(str(last_price))
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
            tf_5m=_candle(close_5m),
            tf_15m=_candle(close_15m),
            tf_1h=_candle(close_1h),
            tf_4h=_candle(close_4h),
        ),
        volume=Decimal("1000"),
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
    def test_signal_in_range_price_above_mean(self) -> None:
        snap = _snapshot(95000, 94000, 93000, 92000, last_price=96000)
        signal = compute_mean_reversion_signal(snap)
        assert -1.0 <= signal <= 1.0

    def test_signal_in_range_price_below_mean(self) -> None:
        snap = _snapshot(95000, 94000, 93000, 92000, last_price=90000)
        signal = compute_mean_reversion_signal(snap)
        assert -1.0 <= signal <= 1.0

    def test_signal_in_range_extreme_deviation(self) -> None:
        snap = _snapshot(50000, 50000, 50000, 49000, last_price=100000)
        signal = compute_mean_reversion_signal(snap)
        assert -1.0 <= signal <= 1.0


# ---------------------------------------------------------------------------
# Dirección de la señal
# ---------------------------------------------------------------------------


class TestSignalDirection:
    def test_price_above_mean_is_bearish(self) -> None:
        # closes = [95000, 94000, 93000, 92000] → mean = 93500
        snap = _snapshot(95000, 94000, 93000, 92000, last_price=96000)
        signal = compute_mean_reversion_signal(snap)
        assert signal < 0.0, "precio sobre la media debe dar señal negativa (bajista)"

    def test_price_below_mean_is_bullish(self) -> None:
        # closes = [95000, 94000, 93000, 92000] → mean = 93500
        snap = _snapshot(95000, 94000, 93000, 92000, last_price=91000)
        signal = compute_mean_reversion_signal(snap)
        assert signal > 0.0, "precio bajo la media debe dar señal positiva (alcista)"

    def test_price_exactly_at_computed_mean_gives_zero(self) -> None:
        # closes = [96000, 94000, 96000, 94000] → mean = 95000
        snap = _snapshot(96000, 94000, 96000, 94000, last_price=95000)
        signal = compute_mean_reversion_signal(snap)
        assert signal == pytest.approx(0.0, abs=1e-9)

    def test_higher_deviation_yields_stronger_signal(self) -> None:
        closes = (95000, 94000, 93000, 92000)  # mean = 93500
        snap_moderate = _snapshot(*closes, last_price=96000)
        snap_strong = _snapshot(*closes, last_price=98000)
        signal_moderate = compute_mean_reversion_signal(snap_moderate)
        signal_strong = compute_mean_reversion_signal(snap_strong)
        assert signal_strong < signal_moderate


# ---------------------------------------------------------------------------
# Mercado plano
# ---------------------------------------------------------------------------


class TestFlatMarket:
    def test_all_closes_identical_returns_zero(self) -> None:
        snap = _snapshot(50000, 50000, 50000, 50000, last_price=50000)
        assert compute_mean_reversion_signal(snap) == 0.0

    def test_near_zero_std_returns_zero(self) -> None:
        # std/mean < _MIN_CV
        base = 50000.0
        tiny = base * _MIN_CV * 0.5
        snap = _snapshot(base, base + tiny, base, base - tiny, last_price=base)
        assert compute_mean_reversion_signal(snap) == 0.0


# ---------------------------------------------------------------------------
# Normalización tanh
# ---------------------------------------------------------------------------


class TestTanhNormalization:
    def test_signal_uses_tanh_shape(self) -> None:
        # Para desviación moderada, la señal debe ser estrictamente menor que 1.0
        snap = _snapshot(95000, 94000, 93000, 92000, last_price=96000)
        signal = compute_mean_reversion_signal(snap)
        assert abs(signal) < 1.0, "tanh no debe saturar en una desviación moderada"

    def test_extreme_price_saturates_near_minus_one(self) -> None:
        # Precio muy por encima → señal cerca de -1.0
        snap = _snapshot(50000, 49000, 48000, 47000, last_price=500000)
        signal = compute_mean_reversion_signal(snap)
        assert signal == pytest.approx(-1.0, abs=0.001)

    def test_extreme_price_below_saturates_near_plus_one(self) -> None:
        # Precio muy por debajo → señal cerca de +1.0
        snap = _snapshot(50000, 49000, 48000, 47000, last_price=1000)
        signal = compute_mean_reversion_signal(snap)
        assert signal == pytest.approx(1.0, abs=0.001)

    def test_known_zscore_matches_tanh(self) -> None:
        # closes = [96000, 94000, 96000, 94000] → mean = 95000
        # std = sqrt(((96000-95000)^2 + (94000-95000)^2)*2 / 4) = 1000
        # last_price = 97000 → z = (95000-97000)/1000 = -2.0 → tanh(-2) ≈ -0.9640
        snap = _snapshot(96000, 94000, 96000, 94000, last_price=97000)
        signal = compute_mean_reversion_signal(snap)
        assert signal == pytest.approx(math.tanh(-2.0), rel=1e-6)


# ---------------------------------------------------------------------------
# Distintos símbolos / precios
# ---------------------------------------------------------------------------


class TestSymbolVariety:
    @pytest.mark.parametrize(
        "closes,last_price",
        [
            ((3000, 2900, 2800, 2700), 3100),   # ETH-like, precio sobre media
            ((100, 99, 98, 97), 95),             # BNB-like, precio bajo media
            ((2.0, 1.9, 1.8, 1.7), 2.1),        # XRP-like, precio sobre media
        ],
    )
    def test_signal_direction_across_price_levels(
        self, closes: tuple[float, float, float, float], last_price: float
    ) -> None:
        snap = _snapshot(*closes, last_price=last_price)
        signal = compute_mean_reversion_signal(snap)
        mean = sum(closes) / len(closes)
        if last_price > mean:
            assert signal < 0.0
        else:
            assert signal > 0.0
