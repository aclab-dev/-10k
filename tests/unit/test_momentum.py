"""Tests unitarios para la señal momentum multi-timeframe (F5, tarjeta 47)."""

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
from backend.quant_signals.engine import compute_quant_signals
from backend.quant_signals.momentum import calculate_momentum

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(open_price: float, close_price: float, n_candles: int = 10) -> CandleData:
    """Construye una CandleData válida con open y close dados."""
    d_open = Decimal(str(open_price))
    d_close = Decimal(str(close_price))
    high = max(d_open, d_close) * Decimal("1.001")
    low = min(d_open, d_close) * Decimal("0.999")
    return CandleData(
        open=d_open,
        high=high,
        low=low,
        close=d_close,
        volume=Decimal("500"),
        n_candles=n_candles,
    )


def _flat_candle(price: float = 50000.0) -> CandleData:
    return _candle(open_price=price, close_price=price)


def _bullish_candle(open_price: float = 50000.0, change_pct: float = 0.01) -> CandleData:
    return _candle(open_price=open_price, close_price=open_price * (1 + change_pct))


def _bearish_candle(open_price: float = 50000.0, change_pct: float = 0.01) -> CandleData:
    return _candle(open_price=open_price, close_price=open_price * (1 - change_pct))


def _snapshot(
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
) -> MarketSnapshot:
    tf_5m = tf_5m or _flat_candle()
    tf_15m = tf_15m or _flat_candle()
    tf_1h = tf_1h or _flat_candle()
    tf_4h = tf_4h or _flat_candle()
    now = _now()
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=Decimal("50005"),
        bid=Decimal("50000"),
        ask=Decimal("50010"),
        spread_absolute=Decimal("10"),
        spread_percent=Decimal("0.02"),
        candles=Candles(tf_5m=tf_5m, tf_15m=tf_15m, tf_1h=tf_1h, tf_4h=tf_4h),
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
# Rango de la señal
# ---------------------------------------------------------------------------


class TestMomentumSignalRange:
    def test_signal_within_bounds_flat_market(self) -> None:
        result = calculate_momentum(_snapshot())
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_strong_bull(self) -> None:
        # Movimientos muy grandes en todos los TFs
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.10),
            tf_15m=_bullish_candle(change_pct=0.10),
            tf_1h=_bullish_candle(change_pct=0.10),
            tf_4h=_bullish_candle(change_pct=0.10),
        )
        result = calculate_momentum(snap)
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_strong_bear(self) -> None:
        snap = _snapshot(
            tf_5m=_bearish_candle(change_pct=0.10),
            tf_15m=_bearish_candle(change_pct=0.10),
            tf_1h=_bearish_candle(change_pct=0.10),
            tf_4h=_bearish_candle(change_pct=0.10),
        )
        result = calculate_momentum(snap)
        assert -1.0 <= result.signal <= 1.0


# ---------------------------------------------------------------------------
# Direccionalidad
# ---------------------------------------------------------------------------


class TestMomentumDirectionality:
    def test_all_bullish_tfs_produce_positive_signal(self) -> None:
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.005),
            tf_15m=_bullish_candle(change_pct=0.005),
            tf_1h=_bullish_candle(change_pct=0.015),
            tf_4h=_bullish_candle(change_pct=0.030),
        )
        result = calculate_momentum(snap)
        assert result.signal > 0.0

    def test_all_bearish_tfs_produce_negative_signal(self) -> None:
        snap = _snapshot(
            tf_5m=_bearish_candle(change_pct=0.005),
            tf_15m=_bearish_candle(change_pct=0.005),
            tf_1h=_bearish_candle(change_pct=0.015),
            tf_4h=_bearish_candle(change_pct=0.030),
        )
        result = calculate_momentum(snap)
        assert result.signal < 0.0

    def test_flat_market_produces_near_zero_signal(self) -> None:
        result = calculate_momentum(_snapshot())
        assert abs(result.signal) < 1e-10

    def test_strong_bull_signal_substantially_positive(self) -> None:
        # Todos los TFs con movimiento igual a su umbral → tanh(1) ≈ 0.76 por TF
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.002),
            tf_15m=_bullish_candle(change_pct=0.004),
            tf_1h=_bullish_candle(change_pct=0.010),
            tf_4h=_bullish_candle(change_pct=0.020),
        )
        result = calculate_momentum(snap)
        assert result.signal > 0.5

    def test_strong_bear_signal_substantially_negative(self) -> None:
        snap = _snapshot(
            tf_5m=_bearish_candle(change_pct=0.002),
            tf_15m=_bearish_candle(change_pct=0.004),
            tf_1h=_bearish_candle(change_pct=0.010),
            tf_4h=_bearish_candle(change_pct=0.020),
        )
        result = calculate_momentum(snap)
        assert result.signal < -0.5

    def test_bearish_signal_is_negation_of_symmetric_bullish(self) -> None:
        # Por simetría del tanh, señal con ROC negativo = -señal con ROC positivo
        bull = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.003),
            tf_15m=_bullish_candle(change_pct=0.006),
            tf_1h=_bullish_candle(change_pct=0.012),
            tf_4h=_bullish_candle(change_pct=0.025),
        )
        bear = _snapshot(
            tf_5m=_bearish_candle(change_pct=0.003),
            tf_15m=_bearish_candle(change_pct=0.006),
            tf_1h=_bearish_candle(change_pct=0.012),
            tf_4h=_bearish_candle(change_pct=0.025),
        )
        assert math.isclose(
            calculate_momentum(bull).signal,
            -calculate_momentum(bear).signal,
            rel_tol=1e-9,
        )


# ---------------------------------------------------------------------------
# Ponderación por timeframe
# ---------------------------------------------------------------------------


class TestMomentumWeighting:
    def test_dominant_4h_bull_overrides_minor_5m_bear(self) -> None:
        # 4h fuertemente bullish (peso 0.40), 5m ligeramente bearish (peso 0.10)
        snap = _snapshot(
            tf_5m=_bearish_candle(change_pct=0.001),
            tf_15m=_flat_candle(),
            tf_1h=_flat_candle(),
            tf_4h=_bullish_candle(change_pct=0.030),
        )
        result = calculate_momentum(snap)
        assert result.signal > 0.0

    def test_dominant_4h_bear_overrides_minor_5m_bull(self) -> None:
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.001),
            tf_15m=_flat_candle(),
            tf_1h=_flat_candle(),
            tf_4h=_bearish_candle(change_pct=0.030),
        )
        result = calculate_momentum(snap)
        assert result.signal < 0.0

    def test_larger_move_on_higher_tf_produces_stronger_signal(self) -> None:
        # Mismo movimiento relativo pero el TF más largo tiene más peso
        snap_4h_dominant = _snapshot(
            tf_5m=_flat_candle(),
            tf_15m=_flat_candle(),
            tf_1h=_flat_candle(),
            tf_4h=_bullish_candle(change_pct=0.020),
        )
        snap_5m_dominant = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.020),
            tf_15m=_flat_candle(),
            tf_1h=_flat_candle(),
            tf_4h=_flat_candle(),
        )
        # El 4h (peso 0.40) con el mismo movimiento produce señal más fuerte que el 5m (0.10)
        r_4h = calculate_momentum(snap_4h_dominant).signal
        r_5m = calculate_momentum(snap_5m_dominant).signal
        assert r_4h > r_5m


# ---------------------------------------------------------------------------
# Determinismo y reproducibilidad
# ---------------------------------------------------------------------------


class TestMomentumDeterminism:
    def test_same_snapshot_produces_same_result(self) -> None:
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.003),
            tf_15m=_bearish_candle(change_pct=0.002),
            tf_1h=_bullish_candle(change_pct=0.008),
            tf_4h=_bullish_candle(change_pct=0.018),
        )
        r1 = calculate_momentum(snap)
        r2 = calculate_momentum(snap)
        assert r1.signal == r2.signal

    def test_result_is_immutable(self) -> None:
        result = calculate_momentum(_snapshot())
        with pytest.raises(AttributeError):
            result.signal = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Estructura del rationale
# ---------------------------------------------------------------------------


class TestMomentumRationale:
    def test_rationale_contains_method(self) -> None:
        result = calculate_momentum(_snapshot())
        assert result.rationale["method"] == "weighted_tanh_roc"

    def test_rationale_contains_all_timeframes(self) -> None:
        result = calculate_momentum(_snapshot())
        tfs = result.rationale["timeframes"]
        assert set(tfs.keys()) == {"5m", "15m", "1h", "4h"}

    def test_rationale_per_tf_has_required_keys(self) -> None:
        result = calculate_momentum(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert "roc" in tf_data
            assert "normalized" in tf_data
            assert "weight" in tf_data
            assert "threshold" in tf_data

    def test_rationale_roc_is_zero_for_flat_candle(self) -> None:
        result = calculate_momentum(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert tf_data["roc"] == 0.0

    def test_rationale_weights_sum_to_one(self) -> None:
        result = calculate_momentum(_snapshot())
        total_weight = sum(tf_data["weight"] for tf_data in result.rationale["timeframes"].values())
        assert math.isclose(total_weight, 1.0)

    def test_rationale_normalized_in_range(self) -> None:
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.005),
            tf_15m=_bearish_candle(change_pct=0.003),
            tf_1h=_bullish_candle(change_pct=0.020),
            tf_4h=_bearish_candle(change_pct=0.010),
        )
        result = calculate_momentum(snap)
        for tf_data in result.rationale["timeframes"].values():
            assert -1.0 <= tf_data["normalized"] <= 1.0

    def test_rationale_signal_matches_result_signal(self) -> None:
        snap = _snapshot(tf_4h=_bullish_candle(change_pct=0.015))
        result = calculate_momentum(snap)
        assert result.rationale["signal"] == result.signal


# ---------------------------------------------------------------------------
# Engine: compute_quant_signals
# ---------------------------------------------------------------------------


class TestComputeQuantSignals:
    def test_returns_valid_quant_signals_package(self) -> None:
        snap = _snapshot(tf_4h=_bullish_candle(change_pct=0.020))
        pkg = compute_quant_signals(snap)
        assert pkg.symbol == "BTCUSDT"
        assert pkg.snapshot_id == snap.snapshot_id
        assert pkg.timestamp_utc == snap.timestamp_utc

    def test_momentum_signal_is_populated(self) -> None:
        snap = _snapshot(tf_4h=_bullish_candle(change_pct=0.020))
        pkg = compute_quant_signals(snap)
        assert pkg.momentum_signal is not None
        assert -1.0 <= pkg.momentum_signal <= 1.0

    def test_other_signals_are_none(self) -> None:
        pkg = compute_quant_signals(_snapshot())
        assert pkg.mean_reversion_signal is None
        assert pkg.breakout_signal is None
        assert pkg.funding_signal is None
        assert pkg.open_interest_signal is None
        assert pkg.order_flow_imbalance_signal is None
        assert pkg.liquidity_sweep_signal is None

    def test_timeframes_used_are_all_four(self) -> None:
        pkg = compute_quant_signals(_snapshot())
        assert set(pkg.timeframes_used) == {"5m", "15m", "1h", "4h"}

    def test_raw_feature_refs_contains_momentum_rationale(self) -> None:
        snap = _snapshot(tf_1h=_bullish_candle(change_pct=0.010))
        pkg = compute_quant_signals(snap)
        assert pkg.raw_feature_refs is not None
        assert "momentum" in pkg.raw_feature_refs
        assert pkg.raw_feature_refs["momentum"]["method"] == "weighted_tanh_roc"

    def test_bullish_snapshot_produces_positive_momentum(self) -> None:
        snap = _snapshot(
            tf_5m=_bullish_candle(change_pct=0.002),
            tf_15m=_bullish_candle(change_pct=0.004),
            tf_1h=_bullish_candle(change_pct=0.010),
            tf_4h=_bullish_candle(change_pct=0.020),
        )
        pkg = compute_quant_signals(snap)
        assert pkg.momentum_signal is not None
        assert pkg.momentum_signal > 0.0
