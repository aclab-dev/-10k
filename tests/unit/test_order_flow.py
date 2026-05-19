"""Tests unitarios para la señal order flow imbalance (F5, tarjeta 51)."""

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
from backend.quant_signals.engine import compute_quant_signals
from backend.quant_signals.order_flow import (
    _TF_WEIGHTS,
    ORDER_FLOW_TIMEFRAMES,
    _candle_ofi,
    calculate_order_flow_imbalance,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(
    open_price: float,
    close_price: float,
    high_price: float | None = None,
    low_price: float | None = None,
    n_candles: int = 10,
) -> CandleData:
    d_open = Decimal(str(open_price))
    d_close = Decimal(str(close_price))
    default_high = max(d_open, d_close) * Decimal("1.001")
    default_low = min(d_open, d_close) * Decimal("0.999")
    d_high = Decimal(str(high_price)) if high_price is not None else default_high
    d_low = Decimal(str(low_price)) if low_price is not None else default_low
    return CandleData(
        open=d_open,
        high=d_high,
        low=d_low,
        close=d_close,
        volume=Decimal("500"),
        n_candles=n_candles,
    )


def _doji_candle(price: float = 50000.0) -> CandleData:
    """Vela doji: high == low == open == close."""
    d = Decimal(str(price))
    return CandleData(open=d, high=d, low=d, close=d, volume=Decimal("100"), n_candles=1)


def _snapshot(
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
) -> MarketSnapshot:
    default = _candle(50000.0, 50000.0)
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
        candles=Candles(
            tf_5m=tf_5m or default,
            tf_15m=tf_15m or default,
            tf_1h=tf_1h or default,
            tf_4h=tf_4h or default,
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
# Tests: _candle_ofi (unidad)
# ---------------------------------------------------------------------------


class TestCandleOfi:
    def test_close_at_high_returns_plus_one(self) -> None:
        # close == high → máxima presión compradora
        c = CandleData(
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("110"),
            volume=Decimal("100"),
            n_candles=1,
        )
        assert _candle_ofi(c) == pytest.approx(1.0)

    def test_close_at_low_returns_minus_one(self) -> None:
        c = CandleData(
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("90"),
            volume=Decimal("100"),
            n_candles=1,
        )
        assert _candle_ofi(c) == pytest.approx(-1.0)

    def test_close_at_midrange_returns_zero(self) -> None:
        c = CandleData(
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("100"),
            volume=Decimal("100"),
            n_candles=1,
        )
        assert _candle_ofi(c) == pytest.approx(0.0)

    def test_doji_returns_zero(self) -> None:
        assert _candle_ofi(_doji_candle()) == pytest.approx(0.0)

    def test_result_always_in_bounds(self) -> None:
        for close in [90, 95, 100, 105, 110]:
            c = CandleData(
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal(str(close)),
                volume=Decimal("100"),
                n_candles=1,
            )
            assert -1.0 <= _candle_ofi(c) <= 1.0


# ---------------------------------------------------------------------------
# Tests: rango de la señal agregada
# ---------------------------------------------------------------------------


class TestOrderFlowSignalRange:
    def test_signal_within_bounds_flat(self) -> None:
        result = calculate_order_flow_imbalance(_snapshot())
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_all_bullish(self) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        snap = _snapshot(tf_5m=bullish, tf_15m=bullish, tf_1h=bullish, tf_4h=bullish)
        result = calculate_order_flow_imbalance(snap)
        assert -1.0 <= result.signal <= 1.0

    def test_signal_within_bounds_all_bearish(self) -> None:
        bearish = _candle(110.0, 90.0, high_price=110.0, low_price=90.0)
        snap = _snapshot(tf_5m=bearish, tf_15m=bearish, tf_1h=bearish, tf_4h=bearish)
        result = calculate_order_flow_imbalance(snap)
        assert -1.0 <= result.signal <= 1.0

    def test_all_doji_returns_zero(self) -> None:
        doji = _doji_candle()
        snap = _snapshot(tf_5m=doji, tf_15m=doji, tf_1h=doji, tf_4h=doji)
        result = calculate_order_flow_imbalance(snap)
        assert result.signal == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests: direccionalidad
# ---------------------------------------------------------------------------


class TestOrderFlowDirectionality:
    def test_all_bullish_closes_produce_positive_signal(self) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        snap = _snapshot(tf_5m=bullish, tf_15m=bullish, tf_1h=bullish, tf_4h=bullish)
        result = calculate_order_flow_imbalance(snap)
        assert result.signal == pytest.approx(1.0)

    def test_all_bearish_closes_produce_negative_signal(self) -> None:
        bearish = _candle(110.0, 90.0, high_price=110.0, low_price=90.0)
        snap = _snapshot(tf_5m=bearish, tf_15m=bearish, tf_1h=bearish, tf_4h=bearish)
        result = calculate_order_flow_imbalance(snap)
        assert result.signal == pytest.approx(-1.0)

    def test_mixed_tfs_uses_weights(self) -> None:
        # Solo 4h es bullish (+1.0, peso 0.40); el resto doji (0.0)
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        doji = _doji_candle()
        snap = _snapshot(tf_5m=doji, tf_15m=doji, tf_1h=doji, tf_4h=bullish)
        result = calculate_order_flow_imbalance(snap)
        assert result.signal == pytest.approx(0.40)

    def test_only_5m_bullish_contributes_its_weight(self) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        doji = _doji_candle()
        snap = _snapshot(tf_5m=bullish, tf_15m=doji, tf_1h=doji, tf_4h=doji)
        result = calculate_order_flow_imbalance(snap)
        assert result.signal == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Tests: rationale / auditabilidad
# ---------------------------------------------------------------------------


class TestOrderFlowRationale:
    def test_rationale_contains_method(self) -> None:
        result = calculate_order_flow_imbalance(_snapshot())
        assert result.rationale["method"] == "weighted_close_position_in_range"

    def test_rationale_contains_all_timeframes(self) -> None:
        result = calculate_order_flow_imbalance(_snapshot())
        assert set(result.rationale["timeframes"].keys()) == {"5m", "15m", "1h", "4h"}

    def test_rationale_timeframe_has_ofi_and_weight(self) -> None:
        result = calculate_order_flow_imbalance(_snapshot())
        for tf_data in result.rationale["timeframes"].values():
            assert "ofi" in tf_data
            assert "weight" in tf_data

    def test_result_is_frozen(self) -> None:
        result = calculate_order_flow_imbalance(_snapshot())
        with pytest.raises((AttributeError, TypeError)):
            result.signal = 0.5  # type: ignore[misc]

    def test_rationale_signal_matches_result_signal(self) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        result = calculate_order_flow_imbalance(_snapshot(tf_4h=bullish))
        assert result.rationale["signal"] == pytest.approx(result.signal)


# ---------------------------------------------------------------------------
# Tests: constantes del módulo
# ---------------------------------------------------------------------------


class TestOrderFlowConstants:
    def test_tf_weights_sum_to_one(self) -> None:
        assert sum(_TF_WEIGHTS.values()) == pytest.approx(1.0)

    def test_timeframes_match_weights_keys(self) -> None:
        assert set(ORDER_FLOW_TIMEFRAMES) == set(_TF_WEIGHTS.keys())


# ---------------------------------------------------------------------------
# Tests: aislamiento por timeframe
# ---------------------------------------------------------------------------


class TestOrderFlowTfIsolation:
    """Verifica que cada TF contribuye con exactamente su peso cuando los demás son doji."""

    @pytest.mark.parametrize(
        "tf, expected_signal",
        [
            ("5m", 0.10),
            ("15m", 0.20),
            ("1h", 0.30),
            ("4h", 0.40),
        ],
    )
    def test_single_bullish_tf_contributes_its_weight(
        self, tf: str, expected_signal: float
    ) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        doji = _doji_candle()
        tfs = ("5m", "15m", "1h", "4h")
        kwargs: dict[str, object] = {f"tf_{t}": (bullish if t == tf else doji) for t in tfs}
        result = calculate_order_flow_imbalance(_snapshot(**kwargs))  # type: ignore[arg-type]
        assert result.signal == pytest.approx(expected_signal)


# ---------------------------------------------------------------------------
# Tests: integración con engine
# ---------------------------------------------------------------------------


class TestOrderFlowEngineIntegration:
    def test_engine_populates_ofi_signal(self) -> None:
        pkg = compute_quant_signals(_snapshot())
        assert pkg.order_flow_imbalance_signal is not None

    def test_engine_ofi_signal_in_bounds(self) -> None:
        bullish = _candle(90.0, 110.0, high_price=110.0, low_price=90.0)
        pkg = compute_quant_signals(_snapshot(tf_4h=bullish))
        assert pkg.order_flow_imbalance_signal is not None
        assert -1.0 <= pkg.order_flow_imbalance_signal <= 1.0

    def test_engine_raw_feature_refs_contains_ofi_rationale(self) -> None:
        pkg = compute_quant_signals(_snapshot())
        assert pkg.raw_feature_refs is not None
        assert "order_flow_imbalance" in pkg.raw_feature_refs
