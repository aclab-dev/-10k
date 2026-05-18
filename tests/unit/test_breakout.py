"""Tests unitarios para la señal breakout detection (F5, tarjeta 49)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.quant_signals.breakout import (
    _INSIDE_ATTENUATION,
    _MIN_VOL_FACTOR,
    calculate_breakout,
)
from backend.quant_signals.engine import compute_quant_signals

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PRICE = 50000.0
_BOX_TOP = 51000.0
_BOX_BOTTOM = 49000.0  # 4h rango: 49000 – 51000


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(
    high: float,
    low: float,
    open_price: float | None = None,
    close_price: float | None = None,
    volume: float = 500.0,
    n_candles: int = 10,
) -> CandleData:
    open_price = open_price or (high + low) / 2
    close_price = close_price or (high + low) / 2
    return CandleData(
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close_price)),
        volume=Decimal(str(volume)),
        n_candles=n_candles,
    )


def _flat_candle(price: float = _BASE_PRICE, volume: float = 500.0) -> CandleData:
    return _candle(
        high=price * 1.001, low=price * 0.999, open_price=price, close_price=price, volume=volume
    )


def _4h_box(volume: float = 1000.0) -> CandleData:
    """Vela 4h que define el rango de referencia: 49000–51000."""
    return _candle(high=_BOX_TOP, low=_BOX_BOTTOM, volume=volume)


def _snapshot(
    last_price: float = _BASE_PRICE,
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
) -> MarketSnapshot:
    """Construye un MarketSnapshot válido con last_price configurable."""
    now = _now()
    lp = Decimal(str(last_price))
    # bid/ask válidos: bid < last_price < ask, spread ≤ 5 %
    bid = lp - Decimal("5")
    ask = lp + Decimal("5")
    spread_abs = ask - bid
    spread_pct = spread_abs / lp * Decimal("100")
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=lp,
        bid=bid,
        ask=ask,
        spread_absolute=spread_abs,
        spread_percent=spread_pct,
        candles=Candles(
            tf_5m=tf_5m or _flat_candle(last_price),
            tf_15m=tf_15m or _flat_candle(last_price),
            tf_1h=tf_1h or _flat_candle(last_price),
            tf_4h=tf_4h or _4h_box(),
        ),
        volume=Decimal("1000"),
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=now,
        local_time=now,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


# ---------------------------------------------------------------------------
# Rango de la señal [-1, 1]
# ---------------------------------------------------------------------------


class TestBreakoutSignalRange:
    def test_signal_within_bounds_inside_range(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BASE_PRICE))
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_above_range(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 500))
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_below_range(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM - 500))
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_extreme_bull(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP * 2))
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_extreme_bear(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM * 0.5))
        assert -1.0 <= result.signal <= 1.0


# ---------------------------------------------------------------------------
# Dirección de la señal
# ---------------------------------------------------------------------------


class TestBreakoutDirection:
    def test_bullish_breakout_produces_positive_signal(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 200))
        assert result.signal > 0.0

    def test_bearish_breakout_produces_negative_signal(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM - 200))
        assert result.signal < 0.0

    def test_larger_breakout_produces_stronger_signal(self) -> None:
        small = calculate_breakout(_snapshot(last_price=_BOX_TOP + 100))
        large = calculate_breakout(_snapshot(last_price=_BOX_TOP + 500))
        assert large.signal > small.signal

    def test_symmetric_breakouts_produce_opposite_signs(self) -> None:
        gap = 300.0
        bull = calculate_breakout(_snapshot(last_price=_BOX_TOP + gap))
        bear = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM - gap))
        assert bull.signal > 0
        assert bear.signal < 0


# ---------------------------------------------------------------------------
# Señal dentro del rango (atenuada)
# ---------------------------------------------------------------------------


class TestBreakoutInsideRange:
    def test_inside_range_signal_is_small(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BASE_PRICE))
        assert abs(result.signal) <= _INSIDE_ATTENUATION + 1e-9

    def test_price_at_midpoint_gives_near_zero(self) -> None:
        mid = (_BOX_TOP + _BOX_BOTTOM) / 2
        result = calculate_breakout(_snapshot(last_price=mid))
        assert abs(result.signal) < 0.01

    def test_price_near_top_inside_gives_positive_signal(self) -> None:
        price = _BOX_TOP - 10  # justo debajo del techo
        result = calculate_breakout(_snapshot(last_price=price))
        assert result.signal > 0

    def test_price_near_bottom_inside_gives_negative_signal(self) -> None:
        price = _BOX_BOTTOM + 10  # justo arriba del piso
        result = calculate_breakout(_snapshot(last_price=price))
        assert result.signal < 0


# ---------------------------------------------------------------------------
# Confirmación por volumen
# ---------------------------------------------------------------------------


class TestVolumeConfirmation:
    def test_high_volume_does_not_exceed_bounds(self) -> None:
        tf_5m_high_vol = _candle(high=_BOX_TOP + 100, low=_BOX_TOP + 50, volume=50000.0)
        result = calculate_breakout(
            _snapshot(
                last_price=_BOX_TOP + 200,
                tf_5m=tf_5m_high_vol,
            )
        )
        assert -1.0 <= result.signal <= 1.0

    def test_zero_baseline_volume_defaults_factor_to_one(self) -> None:
        tf_zero_vol = _candle(high=_BOX_TOP - 10, low=_BOX_BOTTOM + 10, volume=0.0)
        result = calculate_breakout(
            _snapshot(
                last_price=_BOX_TOP + 200,
                tf_1h=tf_zero_vol,
                tf_4h=_candle(high=_BOX_TOP, low=_BOX_BOTTOM, volume=0.0),
            )
        )
        assert result.rationale["volume"]["volume_factor"] == 1.0
        assert -1.0 <= result.signal <= 1.0

    def test_low_volume_dampens_breakout_signal(self) -> None:
        # Mismo breakout alcista, pero 5m con volumen bajo vs normal
        tf_4h = _4h_box(volume=1000.0)
        tf_1h_base = _flat_candle(_BASE_PRICE, volume=600.0)

        normal_vol = _candle(high=_BOX_TOP + 100, low=_BOX_TOP, volume=500.0)
        low_vol = _candle(high=_BOX_TOP + 100, low=_BOX_TOP, volume=10.0)

        result_normal = calculate_breakout(
            _snapshot(last_price=_BOX_TOP + 200, tf_5m=normal_vol, tf_1h=tf_1h_base, tf_4h=tf_4h)
        )
        result_low = calculate_breakout(
            _snapshot(last_price=_BOX_TOP + 200, tf_5m=low_vol, tf_1h=tf_1h_base, tf_4h=tf_4h)
        )
        assert result_low.signal <= result_normal.signal

    def test_volume_factor_floor_is_respected(self) -> None:
        tf_5m_tiny = _candle(high=_BOX_TOP + 100, low=_BOX_TOP, volume=0.001)
        result = calculate_breakout(
            _snapshot(last_price=_BOX_TOP + 200, tf_5m=tf_5m_tiny)
        )
        assert result.rationale["volume"]["volume_factor"] >= _MIN_VOL_FACTOR


# ---------------------------------------------------------------------------
# Determinismo
# ---------------------------------------------------------------------------


class TestBreakoutDeterminism:
    def test_same_snapshot_produces_same_result(self) -> None:
        snap = _snapshot(last_price=_BOX_TOP + 300)
        r1 = calculate_breakout(snap)
        r2 = calculate_breakout(snap)
        assert r1.signal == r2.signal

    def test_different_snapshots_produce_independent_results(self) -> None:
        r_bull = calculate_breakout(_snapshot(last_price=_BOX_TOP + 300))
        r_bear = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM - 300))
        assert r_bull.signal != r_bear.signal


# ---------------------------------------------------------------------------
# Estructura del rationale
# ---------------------------------------------------------------------------


class TestBreakoutRationale:
    def test_rationale_has_required_keys(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 200))
        r = result.rationale
        assert "method" in r
        assert "range" in r
        assert "volume" in r
        assert "raw_breakout" in r
        assert "signal" in r

    def test_rationale_range_has_required_subkeys(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 200))
        rng = result.rationale["range"]
        assert "box_top" in rng
        assert "box_bottom" in rng
        assert "last_price" in rng
        assert "position" in rng
        assert "breakout_pct" in rng

    def test_rationale_volume_has_required_subkeys(self) -> None:
        result = calculate_breakout(_snapshot())
        vol = result.rationale["volume"]
        for key in ("vol_5m_rate_per_min", "baseline_rate_per_min", "vol_ratio", "volume_factor"):
            assert key in vol

    def test_above_range_breakout_pct_is_positive(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 200))
        assert result.rationale["range"]["breakout_pct"] > 0

    def test_below_range_breakout_pct_is_negative(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_BOTTOM - 200))
        assert result.rationale["range"]["breakout_pct"] < 0

    def test_inside_range_breakout_pct_is_none(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BASE_PRICE))
        assert result.rationale["range"]["breakout_pct"] is None

    def test_rationale_signal_matches_result_signal(self) -> None:
        result = calculate_breakout(_snapshot(last_price=_BOX_TOP + 200))
        assert result.rationale["signal"] == result.signal


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestBreakoutEdgeCases:
    def test_flat_4h_candle_price_above_does_not_raise(self) -> None:
        flat_4h = _candle(high=_BASE_PRICE, low=_BASE_PRICE, volume=1000.0)
        snap = _snapshot(last_price=_BASE_PRICE + 100, tf_4h=flat_4h)
        result = calculate_breakout(snap)
        assert -1.0 <= result.signal <= 1.0
        assert result.signal > 0

    def test_flat_4h_candle_price_at_level_gives_zero(self) -> None:
        flat_4h = _candle(high=_BASE_PRICE, low=_BASE_PRICE, volume=1000.0)
        # last_price == box_top == box_bottom: cae en inside_range con mid == last_price
        snap = _snapshot(last_price=_BASE_PRICE, tf_4h=flat_4h)
        result = calculate_breakout(snap)
        assert math.isclose(result.signal, 0.0, abs_tol=1e-9)

    def test_price_exactly_at_box_top_is_inside_range(self) -> None:
        # last_price == box_top no cumple last_price > box_top → inside_range
        snap = _snapshot(last_price=_BOX_TOP)
        result = calculate_breakout(snap)
        assert result.rationale["range"]["position"] == "inside_range"

    def test_price_exactly_at_box_bottom_is_inside_range(self) -> None:
        snap = _snapshot(last_price=_BOX_BOTTOM)
        result = calculate_breakout(snap)
        assert result.rationale["range"]["position"] == "inside_range"


# ---------------------------------------------------------------------------
# Integración con engine
# ---------------------------------------------------------------------------


class TestBreakoutEngineIntegration:
    def test_engine_populates_breakout_signal(self) -> None:
        snap = _snapshot(last_price=_BOX_TOP + 300)
        pkg = compute_quant_signals(snap)
        assert pkg.breakout_signal is not None
        assert -1.0 <= pkg.breakout_signal <= 1.0

    def test_engine_breakout_matches_direct_call(self) -> None:
        snap = _snapshot(last_price=_BOX_TOP + 300)
        pkg = compute_quant_signals(snap)
        direct = calculate_breakout(snap)
        assert pkg.breakout_signal == direct.signal

    def test_engine_raw_feature_refs_includes_breakout(self) -> None:
        snap = _snapshot()
        pkg = compute_quant_signals(snap)
        assert pkg.raw_feature_refs is not None
        assert "breakout" in pkg.raw_feature_refs
        assert "momentum" in pkg.raw_feature_refs
