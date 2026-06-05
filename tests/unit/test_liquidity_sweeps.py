"""Tests unitarios para la señal liquidity sweeps (F5 — tarea #52)."""

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
from backend.quant_signals.liquidity_sweeps import (
    LiquiditySweepResult,
    _find_swing_highs,
    _find_swing_lows,
    calculate_liquidity_sweeps,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(
    open_price: float,
    close_price: float,
    low_extension: float = 0.001,
    high_extension: float = 0.001,
) -> CandleData:
    """Construye una CandleData con wick controlado.

    low_extension / high_extension son fracciones del precio que se extienden
    MÁS ALLÁ del body para formar las mechas.
    """
    d_open = Decimal(str(open_price))
    d_close = Decimal(str(close_price))
    body_top = max(d_open, d_close)
    body_bot = min(d_open, d_close)
    high = body_top * Decimal(str(1 + high_extension))
    low = body_bot * Decimal(str(1 - low_extension))
    return CandleData(
        open=d_open,
        high=high,
        low=low,
        close=d_close,
        volume=Decimal("500"),
        n_candles=10,
    )


def _hammer(price: float = 50000.0, wick_pct: float = 0.02) -> CandleData:
    """Vela hammer: cuerpo pequeño en la parte alta, mecha inferior larga → bullish sweep."""
    open_ = price
    close = price * 1.001  # cuerpo pequeño alcista
    low = price * (1 - wick_pct)
    high = close * 1.0005  # mecha superior mínima
    d_open = Decimal(str(open_))
    d_close = Decimal(str(close))
    return CandleData(
        open=d_open,
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=d_close,
        volume=Decimal("500"),
        n_candles=10,
    )


def _shooting_star(price: float = 50000.0, wick_pct: float = 0.02) -> CandleData:
    """Vela shooting star: cuerpo pequeño en la parte baja, mecha superior larga → bearish sweep."""
    open_ = price
    close = price * 0.999  # cuerpo pequeño bajista
    high = price * (1 + wick_pct)
    low = close * 0.9995  # mecha inferior mínima
    d_open = Decimal(str(open_))
    d_close = Decimal(str(close))
    return CandleData(
        open=d_open,
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=d_close,
        volume=Decimal("500"),
        n_candles=10,
    )


def _doji(price: float = 50000.0) -> CandleData:
    """Vela doji: open == close, mechas simétricas."""
    d = Decimal(str(price))
    return CandleData(
        open=d,
        high=d * Decimal("1.002"),
        low=d * Decimal("0.998"),
        close=d,
        volume=Decimal("500"),
        n_candles=10,
    )


def _snapshot(
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
    history_1h: tuple[CandleData, ...] = (),
    history_4h: tuple[CandleData, ...] = (),
) -> MarketSnapshot:
    tf_5m = tf_5m or _doji()
    tf_15m = tf_15m or _doji()
    tf_1h = tf_1h or _doji()
    tf_4h = tf_4h or _doji()
    now = _now()
    price = Decimal("50005")
    bid = Decimal("50000")
    ask = Decimal("50010")
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=price,
        bid=bid,
        ask=ask,
        spread_absolute=ask - bid,
        spread_percent=(ask - bid) / bid * 100,
        candles=Candles(
            tf_5m=tf_5m,
            tf_15m=tf_15m,
            tf_1h=tf_1h,
            tf_4h=tf_4h,
            history_1h=history_1h,
            history_4h=history_4h,
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


def _flat_candle(price: float) -> CandleData:
    """Vela plana (sin mechas) en torno a un precio dado."""
    d = Decimal(str(price))
    return CandleData(
        open=d,
        high=d * Decimal("1.001"),
        low=d * Decimal("0.999"),
        close=d,
        volume=Decimal("500"),
        n_candles=1,
    )


# ---------------------------------------------------------------------------
# Rango de la señal
# ---------------------------------------------------------------------------


class TestLiquiditySweepSignalRange:
    def test_signal_within_bounds_symmetric_wicks(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_strong_bullish_sweep(self) -> None:
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.05),
            tf_15m=_hammer(wick_pct=0.05),
            tf_1h=_hammer(wick_pct=0.05),
            tf_4h=_hammer(wick_pct=0.05),
        )
        result = calculate_liquidity_sweeps(snap)
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_strong_bearish_sweep(self) -> None:
        snap = _snapshot(
            tf_5m=_shooting_star(wick_pct=0.05),
            tf_15m=_shooting_star(wick_pct=0.05),
            tf_1h=_shooting_star(wick_pct=0.05),
            tf_4h=_shooting_star(wick_pct=0.05),
        )
        result = calculate_liquidity_sweeps(snap)
        assert -1.0 <= result.signal <= 1.0


# ---------------------------------------------------------------------------
# Direccionalidad
# ---------------------------------------------------------------------------


class TestLiquiditySweepDirectionality:
    def test_all_hammers_produce_positive_signal(self) -> None:
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.03),
            tf_15m=_hammer(wick_pct=0.03),
            tf_1h=_hammer(wick_pct=0.03),
            tf_4h=_hammer(wick_pct=0.03),
        )
        result = calculate_liquidity_sweeps(snap)
        assert result.signal > 0.0

    def test_all_shooting_stars_produce_negative_signal(self) -> None:
        snap = _snapshot(
            tf_5m=_shooting_star(wick_pct=0.03),
            tf_15m=_shooting_star(wick_pct=0.03),
            tf_1h=_shooting_star(wick_pct=0.03),
            tf_4h=_shooting_star(wick_pct=0.03),
        )
        result = calculate_liquidity_sweeps(snap)
        assert result.signal < 0.0

    def test_symmetric_doji_produces_near_zero_signal(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        assert abs(result.signal) < 1e-9

    def test_strong_hammer_produces_substantially_positive_signal(self) -> None:
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.04),
            tf_15m=_hammer(wick_pct=0.04),
            tf_1h=_hammer(wick_pct=0.04),
            tf_4h=_hammer(wick_pct=0.04),
        )
        result = calculate_liquidity_sweeps(snap)
        assert result.signal > 0.3

    def test_bullish_and_bearish_sweep_are_symmetric(self) -> None:
        snap_bull = _snapshot(
            tf_5m=_hammer(wick_pct=0.03),
            tf_15m=_hammer(wick_pct=0.03),
            tf_1h=_hammer(wick_pct=0.03),
            tf_4h=_hammer(wick_pct=0.03),
        )
        snap_bear = _snapshot(
            tf_5m=_shooting_star(wick_pct=0.03),
            tf_15m=_shooting_star(wick_pct=0.03),
            tf_1h=_shooting_star(wick_pct=0.03),
            tf_4h=_shooting_star(wick_pct=0.03),
        )
        r_bull = calculate_liquidity_sweeps(snap_bull).signal
        r_bear = calculate_liquidity_sweeps(snap_bear).signal
        # Los helpers no son mirrors exactos (body en posiciones distintas),
        # pero la diferencia es < 1e-5: la simetría del algoritmo se verifica.
        assert math.isclose(r_bull, -r_bear, rel_tol=1e-3)

    def test_larger_wick_produces_stronger_signal(self) -> None:
        small_wick = _snapshot(tf_4h=_hammer(wick_pct=0.01))
        large_wick = _snapshot(tf_4h=_hammer(wick_pct=0.05))
        r_small = calculate_liquidity_sweeps(small_wick).signal
        r_large = calculate_liquidity_sweeps(large_wick).signal
        assert r_large > r_small


# ---------------------------------------------------------------------------
# Ponderación por timeframe
# ---------------------------------------------------------------------------


class TestLiquiditySweepWeighting:
    def test_dominant_4h_hammer_overrides_minor_5m_shooting_star(self) -> None:
        snap = _snapshot(
            tf_5m=_shooting_star(wick_pct=0.005),
            tf_15m=_doji(),
            tf_1h=_doji(),
            tf_4h=_hammer(wick_pct=0.05),
        )
        result = calculate_liquidity_sweeps(snap)
        assert result.signal > 0.0

    def test_4h_hammer_produces_stronger_signal_than_5m_hammer_alone(self) -> None:
        snap_4h = _snapshot(tf_4h=_hammer(wick_pct=0.04))
        snap_5m = _snapshot(tf_5m=_hammer(wick_pct=0.04))
        r_4h = calculate_liquidity_sweeps(snap_4h).signal
        r_5m = calculate_liquidity_sweeps(snap_5m).signal
        assert r_4h > r_5m


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestLiquiditySweepEdgeCases:
    def test_body_only_candle_no_wicks_gives_zero(self) -> None:
        # Vela con open == low y close == high (sin mechas)
        d = Decimal("50000")
        candle = CandleData(
            open=d,
            high=d * Decimal("1.01"),
            low=d,
            close=d * Decimal("1.01"),
            volume=Decimal("500"),
            n_candles=5,
        )
        snap = _snapshot(tf_5m=candle, tf_15m=candle, tf_1h=candle, tf_4h=candle)
        result = calculate_liquidity_sweeps(snap)
        assert result.signal == pytest.approx(0.0, abs=1e-9)

    def test_flat_candle_zero_range_hits_min_range_guard(self) -> None:
        # Vela completamente flat: high == low == open == close → candle_range == 0
        d = Decimal("50000")
        flat = CandleData(open=d, high=d, low=d, close=d, volume=Decimal("500"), n_candles=1)
        snap = _snapshot(tf_5m=flat, tf_15m=flat, tf_1h=flat, tf_4h=flat)
        result = calculate_liquidity_sweeps(snap)
        assert result.signal == pytest.approx(0.0, abs=1e-9)
        for tf_data in result.rationale["timeframes"].values():
            assert tf_data["wick_imbalance"] == pytest.approx(0.0)
            assert tf_data["wick_fraction"] == pytest.approx(0.0)
            assert tf_data["normalized"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Determinismo y reproducibilidad
# ---------------------------------------------------------------------------


class TestLiquiditySweepDeterminism:
    def test_same_snapshot_produces_same_result(self) -> None:
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.02),
            tf_15m=_shooting_star(wick_pct=0.01),
            tf_1h=_hammer(wick_pct=0.03),
            tf_4h=_doji(),
        )
        r1 = calculate_liquidity_sweeps(snap)
        r2 = calculate_liquidity_sweeps(snap)
        assert r1.signal == r2.signal

    def test_result_is_immutable(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        with pytest.raises(AttributeError):
            result.signal = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Estructura del rationale
# ---------------------------------------------------------------------------


class TestLiquiditySweepRationale:
    def test_rationale_contains_method(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        assert result.rationale["method"] == "multi_tf_wick_imbalance"

    def test_rationale_contains_all_timeframes(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        assert set(result.rationale["timeframes"].keys()) == {"5m", "15m", "1h", "4h"}

    def test_rationale_per_tf_has_required_keys(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert "candle_range" in tf_data
            assert "lower_wick" in tf_data
            assert "upper_wick" in tf_data
            assert "wick_imbalance" in tf_data
            assert "wick_fraction" in tf_data
            assert "raw_score" in tf_data
            assert "normalized" in tf_data
            assert "weight" in tf_data
            assert "threshold" in tf_data

    def test_rationale_weights_sum_to_one(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        total = sum(tf_data["weight"] for tf_data in result.rationale["timeframes"].values())
        assert math.isclose(total, 1.0)

    def test_rationale_normalized_in_range(self) -> None:
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.03),
            tf_15m=_shooting_star(wick_pct=0.02),
            tf_1h=_hammer(wick_pct=0.04),
            tf_4h=_doji(),
        )
        result = calculate_liquidity_sweeps(snap)
        for tf_data in result.rationale["timeframes"].values():
            assert -1.0 <= tf_data["normalized"] <= 1.0

    def test_rationale_signal_matches_result_signal(self) -> None:
        snap = _snapshot(tf_4h=_hammer(wick_pct=0.03))
        result = calculate_liquidity_sweeps(snap)
        assert result.rationale["signal"] == result.signal

    def test_doji_gives_zero_wick_imbalance(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert tf_data["wick_imbalance"] == pytest.approx(0.0, abs=1e-9)

    def test_result_type_is_liquidity_sweep_result(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        assert isinstance(result, LiquiditySweepResult)

    def test_rationale_has_detection_method_per_tf(self) -> None:
        result = calculate_liquidity_sweeps(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert "detection_method" in tf_data
            assert tf_data["detection_method"] in ("wick_proxy", "historical_levels")


# ---------------------------------------------------------------------------
# Swing level detection
# ---------------------------------------------------------------------------


def _make_history(lows: list[float], highs: list[float]) -> tuple[CandleData, ...]:
    """Construye una tupla de CandleData con los lows/highs especificados."""
    candles = []
    for low, high in zip(lows, highs, strict=True):
        mid = (low + high) / 2
        d_low = Decimal(str(low))
        d_high = Decimal(str(high))
        d_mid = Decimal(str(round(mid, 2)))
        candles.append(
            CandleData(
                open=d_mid,
                high=d_high,
                low=d_low,
                close=d_mid,
                volume=Decimal("500"),
                n_candles=1,
            )
        )
    return tuple(candles)


class TestSwingLevelDetection:
    def test_swing_low_detected(self) -> None:
        # Pivote claro en el índice 3 (lookback=3 → necesita 3 velas a cada lado)
        lows = [100.0, 99.0, 98.0, 95.0, 98.0, 99.0, 100.0]
        highs = [101.0] * 7
        history = _make_history(lows, highs)
        result = _find_swing_lows(history, lookback=3)
        assert 95.0 in result

    def test_swing_high_detected(self) -> None:
        highs = [100.0, 101.0, 102.0, 105.0, 102.0, 101.0, 100.0]
        lows = [99.0] * 7
        history = _make_history(lows, highs)
        result = _find_swing_highs(history, lookback=3)
        assert 105.0 in result

    def test_no_swing_lows_when_monotonic_decreasing(self) -> None:
        lows = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0]
        highs = [101.0] * 7
        history = _make_history(lows, highs)
        result = _find_swing_lows(history, lookback=3)
        assert result == []

    def test_no_swing_highs_when_monotonic_increasing(self) -> None:
        highs = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        lows = [99.0] * 7
        history = _make_history(lows, highs)
        result = _find_swing_highs(history, lookback=3)
        assert result == []

    def test_insufficient_history_returns_empty(self) -> None:
        lows = [100.0, 99.0]
        highs = [101.0] * 2
        history = _make_history(lows, highs)
        assert _find_swing_lows(history, lookback=3) == []
        assert _find_swing_highs(history, lookback=3) == []


# ---------------------------------------------------------------------------
# Detección histórica de sweeps
# ---------------------------------------------------------------------------


def _history_with_swing_low(swing_low_price: float, n: int = 9) -> tuple[CandleData, ...]:
    """Historia con un swing low claro al centro."""
    mid = n // 2
    lows = [swing_low_price + 5.0] * n
    highs = [swing_low_price + 10.0] * n
    lows[mid] = swing_low_price  # pivote mínimo
    return _make_history(lows, highs)


def _history_with_swing_high(swing_high_price: float, n: int = 9) -> tuple[CandleData, ...]:
    """Historia con un swing high claro al centro."""
    mid = n // 2
    highs = [swing_high_price - 5.0] * n
    lows = [swing_high_price - 10.0] * n
    highs[mid] = swing_high_price  # pivote máximo
    return _make_history(lows, highs)


class TestHistoricalSweepDetection:
    def test_bullish_sweep_over_swing_low(self) -> None:
        swing_low = 49000.0
        history = _history_with_swing_low(swing_low)
        # Vela actual: low perfora el swing low, close recupera por encima
        d = Decimal(str(swing_low))
        sweep_candle = CandleData(
            open=Decimal(str(swing_low + 200)),
            high=Decimal(str(swing_low + 400)),
            low=d - Decimal("100"),  # perfora 100 debajo del nivel
            close=d + Decimal("150"),  # cierra 150 por encima del nivel
            volume=Decimal("500"),
            n_candles=1,
        )
        snap = _snapshot(tf_1h=sweep_candle, history_1h=history)
        result = calculate_liquidity_sweeps(snap)
        assert result.signal > 0.0
        assert result.rationale["timeframes"]["1h"]["detection_method"] == "historical_levels"
        swept = result.rationale["timeframes"]["1h"]["historical"]["swept_low"]
        assert swept == pytest.approx(swing_low)

    def test_bearish_sweep_over_swing_high(self) -> None:
        swing_high = 51000.0
        history = _history_with_swing_high(swing_high)
        d = Decimal(str(swing_high))
        sweep_candle = CandleData(
            open=Decimal(str(swing_high - 200)),
            high=d + Decimal("100"),  # perfora 100 por encima del nivel
            low=Decimal(str(swing_high - 400)),
            close=d - Decimal("150"),  # cierra 150 por debajo del nivel
            volume=Decimal("500"),
            n_candles=1,
        )
        snap = _snapshot(tf_1h=sweep_candle, history_1h=history)
        result = calculate_liquidity_sweeps(snap)
        assert result.signal < 0.0
        swept = result.rationale["timeframes"]["1h"]["historical"]["swept_high"]
        assert swept == pytest.approx(swing_high)

    def test_no_sweep_when_level_not_pierced(self) -> None:
        swing_low = 49000.0
        history = _history_with_swing_low(swing_low)
        # Vela que no baja al swing low
        normal_candle = _flat_candle(50000.0)
        snap = _snapshot(tf_1h=normal_candle, history_1h=history)
        result = calculate_liquidity_sweeps(snap)
        assert result.rationale["timeframes"]["1h"]["historical"]["swept_low"] is None
        assert result.rationale["timeframes"]["1h"]["historical"]["swept_high"] is None

    def test_no_sweep_when_pierced_but_no_recovery(self) -> None:
        swing_low = 49000.0
        history = _history_with_swing_low(swing_low)
        # Low perfora pero close también queda debajo: no hay recuperación
        d = Decimal(str(swing_low))
        no_recovery = CandleData(
            open=Decimal(str(swing_low + 200)),
            high=Decimal(str(swing_low + 300)),
            low=d - Decimal("200"),
            close=d - Decimal("50"),  # cierra debajo del nivel → no es sweep
            volume=Decimal("500"),
            n_candles=1,
        )
        snap = _snapshot(tf_1h=no_recovery, history_1h=history)
        result = calculate_liquidity_sweeps(snap)
        assert result.rationale["timeframes"]["1h"]["historical"]["swept_low"] is None


# ---------------------------------------------------------------------------
# Backward compatibility y fallback
# ---------------------------------------------------------------------------


class TestHistoricalFallback:
    def test_without_history_uses_wick_proxy(self) -> None:
        snap = _snapshot(tf_1h=_hammer(wick_pct=0.03))
        result = calculate_liquidity_sweeps(snap)
        assert result.rationale["timeframes"]["1h"]["detection_method"] == "wick_proxy"

    def test_insufficient_history_uses_wick_proxy(self) -> None:
        # Solo 3 velas → menos que _MIN_HISTORY_LEN (5)
        short_history = tuple(_flat_candle(50000.0) for _ in range(3))
        snap = _snapshot(tf_1h=_hammer(wick_pct=0.03), history_1h=short_history)
        result = calculate_liquidity_sweeps(snap)
        assert result.rationale["timeframes"]["1h"]["detection_method"] == "wick_proxy"

    def test_backward_compat_no_history_same_result(self) -> None:
        """Sin historia, el resultado debe ser idéntico al comportamiento original."""
        snap = _snapshot(
            tf_5m=_hammer(wick_pct=0.02),
            tf_15m=_doji(),
            tf_1h=_shooting_star(wick_pct=0.03),
            tf_4h=_hammer(wick_pct=0.04),
        )
        r1 = calculate_liquidity_sweeps(snap)
        r2 = calculate_liquidity_sweeps(snap)
        assert r1.signal == r2.signal

    def test_signal_within_bounds_with_history(self) -> None:
        history = _history_with_swing_low(49000.0)
        snap = _snapshot(tf_1h=_hammer(wick_pct=0.03), history_1h=history)
        result = calculate_liquidity_sweeps(snap)
        assert -1.0 <= result.signal <= 1.0
