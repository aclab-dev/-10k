"""Tests de señales sobre series sintéticas conocidas — trending, ranging, breakout, etc.

Cada serie es una lista de MarketSnapshot que representa un régimen de mercado
reconocible. Los tests verifican que cada señal responde con el signo, magnitud
y consistencia esperados a lo largo de la serie.

Series cubiertas:
  trending_up / trending_down  — precio e intra-candle consistentemente direccionales
  ranging                      — precio oscilando dentro de un rango fijo
  breakout_bullish/bearish     — precio rompiendo el rango 4h
  sweep_bullish/bearish        — velas con mechas asimétricas (stop hunts)
  funding extremos             — funding rate muy positivo o muy negativo
  oi extremos                  — open interest relativo a volumen muy alto o muy bajo
"""

from __future__ import annotations

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
from backend.quant_signals.breakout import calculate_breakout
from backend.quant_signals.funding import compute_funding_signal
from backend.quant_signals.liquidity_sweeps import calculate_liquidity_sweeps
from backend.quant_signals.mean_reversion import compute_mean_reversion_signal
from backend.quant_signals.momentum import calculate_momentum
from backend.quant_signals.open_interest import compute_open_interest_signal
from backend.quant_signals.order_flow import calculate_order_flow_imbalance

# ---------------------------------------------------------------------------
# Builders primitivos
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle(
    open_: float,
    close: float,
    wick_pct: float = 0.001,
    volume: float = 500.0,
) -> CandleData:
    """Vela OHLCV con mechas simétricas mínimas."""
    o = Decimal(str(open_))
    c = Decimal(str(close))
    high = max(o, c) * Decimal(str(1 + wick_pct))
    low = min(o, c) * Decimal(str(1 - wick_pct))
    return CandleData(
        open=o, high=high, low=low, close=c, volume=Decimal(str(volume)), n_candles=10
    )


def _candle_bullish_sweep(price: float = 50000.0) -> CandleData:
    """Vela con mecha inferior larga y mecha superior mínima (bullish sweep)."""
    o = Decimal(str(price))
    c = Decimal(str(price * 1.002))
    high = Decimal(str(price * 1.003))
    low = Decimal(str(price * 0.984))
    return CandleData(open=o, high=high, low=low, close=c, volume=Decimal("1000"), n_candles=10)


def _candle_bearish_sweep(price: float = 50000.0) -> CandleData:
    """Vela con mecha superior larga y mecha inferior mínima (bearish sweep)."""
    o = Decimal(str(price))
    c = Decimal(str(price * 0.998))
    high = Decimal(str(price * 1.016))
    low = Decimal(str(price * 0.997))
    return CandleData(open=o, high=high, low=low, close=c, volume=Decimal("1000"), n_candles=10)


def _snapshot(
    last_price: float,
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
    funding_rate: float | None = None,
    open_interest: float | None = None,
    volume: float = 1000.0,
) -> MarketSnapshot:
    lp = Decimal(str(last_price))
    flat = _candle(last_price, last_price)
    now = _now()
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=lp,
        bid=lp * Decimal("0.9999"),
        ask=lp * Decimal("1.0001"),
        spread_absolute=lp * Decimal("0.0002"),
        spread_percent=Decimal("0.02"),
        candles=Candles(
            tf_5m=tf_5m or flat,
            tf_15m=tf_15m or flat,
            tf_1h=tf_1h or flat,
            tf_4h=tf_4h or flat,
        ),
        volume=Decimal(str(volume)),
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=10,
        exchange_server_time=now,
        local_time=now,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
        funding_rate=funding_rate,
        open_interest=Decimal(str(open_interest)) if open_interest is not None else None,
    )


# ---------------------------------------------------------------------------
# Generadores de series sintéticas
# ---------------------------------------------------------------------------


def _trending_up_series(
    n: int = 5, base: float = 50000.0, step_pct: float = 0.015
) -> list[MarketSnapshot]:
    """n snapshots con candles y precios consistentemente alcistas en todos los TFs."""
    series = []
    for i in range(n):
        price = base * (1 + step_pct * i)
        open_ = price * (1 - step_pct / 2)
        close = price * (1 + step_pct / 2)
        c = _candle(open_, close)
        series.append(_snapshot(last_price=close, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c))
    return series


def _trending_down_series(
    n: int = 5, base: float = 50000.0, step_pct: float = 0.015
) -> list[MarketSnapshot]:
    """n snapshots con candles y precios consistentemente bajistas en todos los TFs."""
    series = []
    for i in range(n):
        price = base * (1 - step_pct * i)
        open_ = price * (1 + step_pct / 2)
        close = price * (1 - step_pct / 2)
        c = _candle(open_, close)
        series.append(_snapshot(last_price=close, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c))
    return series


def _ranging_series(
    n: int = 6, base: float = 50000.0, amplitude_pct: float = 0.003
) -> list[MarketSnapshot]:
    """n snapshots alternando entre alza y baja leve dentro de un rango."""
    series = []
    for i in range(n):
        direction = 1.0 if i % 2 == 0 else -1.0
        close = base * (1 + direction * amplitude_pct)
        c = _candle(base, close)
        series.append(_snapshot(last_price=close, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c))
    return series


def _breakout_bullish_series(base: float = 50000.0) -> list[MarketSnapshot]:
    """4 snapshots: 2 dentro del rango 4h, 2 rompiendo hacia arriba."""
    box_high = base * 1.01
    box_low = base * 0.99
    tf_4h = CandleData(
        open=Decimal(str(base)),
        high=Decimal(str(box_high)),
        low=Decimal(str(box_low)),
        close=Decimal(str(base)),
        volume=Decimal("5000"),
        n_candles=10,
    )
    return [
        _snapshot(last_price=base, tf_4h=tf_4h),
        _snapshot(last_price=base * 1.005, tf_4h=tf_4h),
        _snapshot(last_price=box_high * 1.005, tf_4h=tf_4h),
        _snapshot(last_price=box_high * 1.015, tf_4h=tf_4h),
    ]


def _breakout_bearish_series(base: float = 50000.0) -> list[MarketSnapshot]:
    """4 snapshots: 2 dentro del rango 4h, 2 rompiendo hacia abajo."""
    box_high = base * 1.01
    box_low = base * 0.99
    tf_4h = CandleData(
        open=Decimal(str(base)),
        high=Decimal(str(box_high)),
        low=Decimal(str(box_low)),
        close=Decimal(str(base)),
        volume=Decimal("5000"),
        n_candles=10,
    )
    return [
        _snapshot(last_price=base, tf_4h=tf_4h),
        _snapshot(last_price=base * 0.995, tf_4h=tf_4h),
        _snapshot(last_price=box_low * 0.995, tf_4h=tf_4h),
        _snapshot(last_price=box_low * 0.985, tf_4h=tf_4h),
    ]


def _sweep_bullish_series(n: int = 4, base: float = 50000.0) -> list[MarketSnapshot]:
    """n snapshots con velas de mecha inferior dominante (bullish sweep)."""
    c = _candle_bullish_sweep(base)
    return [_snapshot(last_price=base, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c) for _ in range(n)]


def _sweep_bearish_series(n: int = 4, base: float = 50000.0) -> list[MarketSnapshot]:
    """n snapshots con velas de mecha superior dominante (bearish sweep)."""
    c = _candle_bearish_sweep(base)
    return [_snapshot(last_price=base, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c) for _ in range(n)]


# ---------------------------------------------------------------------------
# Tests — Momentum
# ---------------------------------------------------------------------------


class TestMomentumSyntheticSeries:
    def test_trending_up_produces_consistently_positive_signal(self) -> None:
        for snap in _trending_up_series():
            result = calculate_momentum(snap)
            assert result.signal > 0, f"Esperado positivo, got {result.signal}"

    def test_trending_down_produces_consistently_negative_signal(self) -> None:
        for snap in _trending_down_series():
            result = calculate_momentum(snap)
            assert result.signal < 0, f"Esperado negativo, got {result.signal}"

    def test_ranging_signals_have_alternating_signs(self) -> None:
        series = _ranging_series()
        signals = [calculate_momentum(s).signal for s in series]
        for i, sig in enumerate(signals):
            if i % 2 == 0:
                assert sig > 0, f"Snapshot {i}: esperado positivo, got {sig}"
            else:
                assert sig < 0, f"Snapshot {i}: esperado negativo, got {sig}"

    def test_trending_signal_stronger_in_aggregate_than_ranging(self) -> None:
        trend = sum(abs(calculate_momentum(s).signal) for s in _trending_up_series())
        ranging = sum(abs(calculate_momentum(s).signal) for s in _ranging_series())
        assert trend > ranging

    def test_all_trending_up_signals_are_positive(self) -> None:
        assert all(calculate_momentum(s).signal > 0 for s in _trending_up_series(n=5))

    def test_all_trending_down_signals_are_negative(self) -> None:
        assert all(calculate_momentum(s).signal < 0 for s in _trending_down_series(n=5))


# ---------------------------------------------------------------------------
# Tests — Mean Reversion
# ---------------------------------------------------------------------------


class TestMeanReversionSyntheticSeries:
    def test_price_above_all_tf_closes_gives_negative_signal(self) -> None:
        """Precio extendido por encima de todos los cierres → señal bajista (reversa bajista)."""
        base = 50000.0
        snap = _snapshot(
            last_price=base * 1.05,
            tf_5m=_candle(base, base),
            tf_15m=_candle(base * 0.99, base * 0.99),
            tf_1h=_candle(base * 0.98, base * 0.98),
            tf_4h=_candle(base * 0.97, base * 0.97),
        )
        assert compute_mean_reversion_signal(snap) < 0

    def test_price_below_all_tf_closes_gives_positive_signal(self) -> None:
        """Precio extendido por debajo de todos los cierres → señal alcista (reversa alcista)."""
        base = 50000.0
        snap = _snapshot(
            last_price=base * 0.95,
            tf_5m=_candle(base, base),
            tf_15m=_candle(base * 1.01, base * 1.01),
            tf_1h=_candle(base * 1.02, base * 1.02),
            tf_4h=_candle(base * 1.03, base * 1.03),
        )
        assert compute_mean_reversion_signal(snap) > 0

    def test_trending_up_with_divergent_tfs_produces_negative_signal(self) -> None:
        """Tendencia alcista con TFs divergentes: last_price > media de cierres → negativo."""
        base = 50000.0
        step = 0.015
        # TFs escalonados: 5m > 15m > 1h > 4h, last_price por encima de todos
        snap = _snapshot(
            last_price=base * (1 + step * 3) * 1.008,
            tf_5m=_candle(base * (1 + step * 2.5), base * (1 + step * 3)),
            tf_15m=_candle(base * (1 + step * 1.5), base * (1 + step * 2)),
            tf_1h=_candle(base * (1 + step * 0.5), base * (1 + step)),
            tf_4h=_candle(base * 0.995, base),
        )
        assert compute_mean_reversion_signal(snap) < 0

    def test_trending_down_with_divergent_tfs_produces_positive_signal(self) -> None:
        """Tendencia bajista donde last_price < media de los cierres TF → señal positiva."""
        base = 50000.0
        step = 0.015
        snap = _snapshot(
            last_price=base * (1 - step * 3) * 0.992,
            tf_5m=_candle(base * (1 - step * 2.5), base * (1 - step * 3)),
            tf_15m=_candle(base * (1 - step * 1.5), base * (1 - step * 2)),
            tf_1h=_candle(base * (1 - step * 0.5), base * (1 - step)),
            tf_4h=_candle(base * 1.005, base),
        )
        assert compute_mean_reversion_signal(snap) > 0

    def test_flat_market_produces_near_zero_signal(self) -> None:
        """Todos los TFs con el mismo precio y last_price igual → señal ≈ 0."""
        price = 50000.0
        snap = _snapshot(
            last_price=price,
            tf_5m=_candle(price, price),
            tf_15m=_candle(price, price),
            tf_1h=_candle(price, price),
            tf_4h=_candle(price, price),
        )
        assert abs(compute_mean_reversion_signal(snap)) < 0.01

    def test_stronger_deviation_produces_stronger_signal(self) -> None:
        """Cuanto más se aleja last_price de la media, mayor la magnitud de la señal."""
        base = 50000.0
        closes = [base, base * 0.99, base * 0.98, base * 0.97]
        snap_mild = _snapshot(
            last_price=base * 1.02,
            tf_5m=_candle(closes[0], closes[0]),
            tf_15m=_candle(closes[1], closes[1]),
            tf_1h=_candle(closes[2], closes[2]),
            tf_4h=_candle(closes[3], closes[3]),
        )
        snap_strong = _snapshot(
            last_price=base * 1.10,
            tf_5m=_candle(closes[0], closes[0]),
            tf_15m=_candle(closes[1], closes[1]),
            tf_1h=_candle(closes[2], closes[2]),
            tf_4h=_candle(closes[3], closes[3]),
        )
        assert abs(compute_mean_reversion_signal(snap_strong)) > abs(
            compute_mean_reversion_signal(snap_mild)
        )


# ---------------------------------------------------------------------------
# Tests — Breakout
# ---------------------------------------------------------------------------


class TestBreakoutSyntheticSeries:
    def test_inside_range_produces_attenuated_signal(self) -> None:
        for snap in _breakout_bullish_series()[:2]:
            assert abs(calculate_breakout(snap).signal) <= 0.15

    def test_breakout_bullish_outside_produces_positive_signal(self) -> None:
        for snap in _breakout_bullish_series()[2:]:
            assert calculate_breakout(snap).signal > 0.3

    def test_breakout_bearish_outside_produces_negative_signal(self) -> None:
        for snap in _breakout_bearish_series()[2:]:
            assert calculate_breakout(snap).signal < -0.3

    def test_ranging_series_stays_inside_range(self) -> None:
        """Mercado ranging: señal breakout queda cerca de 0 en todos los snapshots."""
        for snap in _ranging_series():
            assert abs(calculate_breakout(snap).signal) <= 0.15

    def test_transition_from_inside_to_outside_increases_signal(self) -> None:
        series = _breakout_bullish_series()
        signals = [calculate_breakout(s).signal for s in series]
        assert signals[2] > signals[1]
        assert signals[3] > signals[1]

    def test_signal_increases_with_distance_from_range(self) -> None:
        base = 50000.0
        box_high = base * 1.01
        tf_4h = CandleData(
            open=Decimal(str(base)),
            high=Decimal(str(box_high)),
            low=Decimal(str(base * 0.99)),
            close=Decimal(str(base)),
            volume=Decimal("5000"),
            n_candles=10,
        )
        small = calculate_breakout(_snapshot(last_price=box_high * 1.005, tf_4h=tf_4h))
        large = calculate_breakout(_snapshot(last_price=box_high * 1.020, tf_4h=tf_4h))
        assert large.signal > small.signal > 0

    def test_bullish_and_bearish_breakouts_have_opposite_signs(self) -> None:
        bull = _breakout_bullish_series()[3]
        bear = _breakout_bearish_series()[3]
        assert calculate_breakout(bull).signal > 0
        assert calculate_breakout(bear).signal < 0


# ---------------------------------------------------------------------------
# Tests — Order Flow Imbalance
# ---------------------------------------------------------------------------


class TestOrderFlowSyntheticSeries:
    def test_trending_up_produces_consistently_positive_signal(self) -> None:
        for snap in _trending_up_series():
            assert calculate_order_flow_imbalance(snap).signal > 0

    def test_trending_down_produces_consistently_negative_signal(self) -> None:
        for snap in _trending_down_series():
            assert calculate_order_flow_imbalance(snap).signal < 0

    def test_ranging_series_oscillates_sign_with_candle_direction(self) -> None:
        series = _ranging_series()
        signals = [calculate_order_flow_imbalance(s).signal for s in series]
        for i, sig in enumerate(signals):
            if i % 2 == 0:
                assert sig > 0, f"Snapshot {i}: esperado positivo, got {sig}"
            else:
                assert sig < 0, f"Snapshot {i}: esperado negativo, got {sig}"

    def test_bullish_candle_has_higher_ofi_than_bearish_candle(self) -> None:
        bull = _trending_up_series(n=1)[0]
        bear = _trending_down_series(n=1)[0]
        assert (
            calculate_order_flow_imbalance(bull).signal
            > calculate_order_flow_imbalance(bear).signal
        )

    def test_trending_aggregate_stronger_than_ranging(self) -> None:
        trend = sum(abs(calculate_order_flow_imbalance(s).signal) for s in _trending_up_series())
        ranging = sum(abs(calculate_order_flow_imbalance(s).signal) for s in _ranging_series())
        assert trend > ranging


# ---------------------------------------------------------------------------
# Tests — Liquidity Sweeps
# ---------------------------------------------------------------------------


class TestLiquiditySweepsSyntheticSeries:
    def test_bullish_sweep_series_produces_positive_signal(self) -> None:
        for snap in _sweep_bullish_series():
            assert calculate_liquidity_sweeps(snap).signal > 0

    def test_bearish_sweep_series_produces_negative_signal(self) -> None:
        for snap in _sweep_bearish_series():
            assert calculate_liquidity_sweeps(snap).signal < 0

    def test_bullish_sweep_signal_stronger_than_symmetric_candle(self) -> None:
        """Vela sweep tiene mechas pronunciadas → señal sweep mayor que vela simétrica."""
        sweep_snap = _sweep_bullish_series(n=1)[0]
        symmetric_snap = _snapshot(
            last_price=50000.0,
            tf_5m=_candle(50000.0, 50000.0, wick_pct=0.01),
            tf_15m=_candle(50000.0, 50000.0, wick_pct=0.01),
            tf_1h=_candle(50000.0, 50000.0, wick_pct=0.01),
            tf_4h=_candle(50000.0, 50000.0, wick_pct=0.01),
        )
        assert (
            calculate_liquidity_sweeps(sweep_snap).signal
            > calculate_liquidity_sweeps(symmetric_snap).signal
        )

    def test_flat_doji_candle_produces_near_zero_sweep(self) -> None:
        price = 50000.0
        c = CandleData(
            open=Decimal(str(price)),
            high=Decimal(str(price * 1.0005)),
            low=Decimal(str(price * 0.9995)),
            close=Decimal(str(price)),
            volume=Decimal("500"),
            n_candles=10,
        )
        snap = _snapshot(last_price=price, tf_5m=c, tf_15m=c, tf_1h=c, tf_4h=c)
        assert abs(calculate_liquidity_sweeps(snap).signal) < 0.1

    def test_stronger_lower_wick_produces_stronger_bullish_signal(self) -> None:
        price = 50000.0
        c_mild = CandleData(
            open=Decimal(str(price)),
            high=Decimal(str(price * 1.002)),
            low=Decimal(str(price * 0.993)),
            close=Decimal(str(price * 1.001)),
            volume=Decimal("500"),
            n_candles=10,
        )
        c_strong = CandleData(
            open=Decimal(str(price)),
            high=Decimal(str(price * 1.002)),
            low=Decimal(str(price * 0.980)),
            close=Decimal(str(price * 1.001)),
            volume=Decimal("500"),
            n_candles=10,
        )
        snap_mild = _snapshot(
            last_price=price, tf_5m=c_mild, tf_15m=c_mild, tf_1h=c_mild, tf_4h=c_mild
        )
        snap_strong = _snapshot(
            last_price=price, tf_5m=c_strong, tf_15m=c_strong, tf_1h=c_strong, tf_4h=c_strong
        )
        assert (
            calculate_liquidity_sweeps(snap_strong).signal
            > calculate_liquidity_sweeps(snap_mild).signal
        )


# ---------------------------------------------------------------------------
# Tests — Funding Rate
# ---------------------------------------------------------------------------


class TestFundingSignalSyntheticSeries:
    def test_high_positive_funding_produces_strong_negative_signal(self) -> None:
        """Funding muy positivo (longs apalancados) → señal bajista fuerte."""
        snap = _snapshot(last_price=50000.0, funding_rate=0.001)
        assert compute_funding_signal(snap) < -0.9

    def test_high_negative_funding_produces_strong_positive_signal(self) -> None:
        """Funding muy negativo (shorts apalancados) → señal alcista fuerte."""
        snap = _snapshot(last_price=50000.0, funding_rate=-0.001)
        assert compute_funding_signal(snap) > 0.9

    def test_neutral_funding_produces_near_zero_signal(self) -> None:
        snap = _snapshot(last_price=50000.0, funding_rate=0.00001)
        assert abs(compute_funding_signal(snap)) < 0.1

    def test_signal_magnitude_increases_with_funding_extremity(self) -> None:
        snap_mild = _snapshot(last_price=50000.0, funding_rate=0.0001)
        snap_extreme = _snapshot(last_price=50000.0, funding_rate=0.001)
        assert abs(compute_funding_signal(snap_extreme)) > abs(compute_funding_signal(snap_mild))

    def test_positive_and_negative_funding_produce_opposite_signs(self) -> None:
        pos = compute_funding_signal(_snapshot(last_price=50000.0, funding_rate=0.0003))
        neg = compute_funding_signal(_snapshot(last_price=50000.0, funding_rate=-0.0003))
        assert pos < 0 and neg > 0

    def test_funding_signal_symmetry(self) -> None:
        """Funding positivo/negativo de igual magnitud produce señales opuestas simétricas."""
        rate = 0.0003
        pos = compute_funding_signal(_snapshot(last_price=50000.0, funding_rate=rate))
        neg = compute_funding_signal(_snapshot(last_price=50000.0, funding_rate=-rate))
        assert abs(pos + neg) < 1e-9

    def test_no_funding_rate_returns_zero(self) -> None:
        snap = _snapshot(last_price=50000.0, funding_rate=None)
        assert compute_funding_signal(snap) == 0.0


# ---------------------------------------------------------------------------
# Tests — Open Interest
# ---------------------------------------------------------------------------


class TestOpenInterestSyntheticSeries:
    def test_high_oi_relative_to_volume_produces_strong_negative_signal(self) -> None:
        """OI >> volumen → mercado sobre-apalancado → señal bajista."""
        snap = _snapshot(last_price=50000.0, open_interest=10000.0, volume=1000.0)
        assert compute_open_interest_signal(snap) < -0.8

    def test_low_oi_relative_to_volume_produces_positive_signal(self) -> None:
        """OI << volumen → mercado des-apalancado → señal alcista.

        Con ratio=0.1 (OI=100, vol=1000): signal = tanh(0.9) ≈ 0.716.
        La función satura despacio desde abajo; el piso práctico es ~0.7 con
        ratio=0.1, y no puede superar 1.0 por definición de tanh.
        """
        snap = _snapshot(last_price=50000.0, open_interest=100.0, volume=1000.0)
        assert compute_open_interest_signal(snap) > 0.7

    def test_equal_oi_and_volume_produces_near_zero_signal(self) -> None:
        snap = _snapshot(last_price=50000.0, open_interest=1000.0, volume=1000.0)
        assert abs(compute_open_interest_signal(snap)) < 0.1

    def test_signal_magnitude_increases_with_oi_imbalance(self) -> None:
        snap_mild = _snapshot(last_price=50000.0, open_interest=2000.0, volume=1000.0)
        snap_extreme = _snapshot(last_price=50000.0, open_interest=10000.0, volume=1000.0)
        assert abs(compute_open_interest_signal(snap_extreme)) > abs(
            compute_open_interest_signal(snap_mild)
        )

    def test_no_oi_returns_zero(self) -> None:
        snap = _snapshot(last_price=50000.0, open_interest=None)
        assert compute_open_interest_signal(snap) == 0.0


# ---------------------------------------------------------------------------
# Tests — Coherencia cruzada entre señales
# ---------------------------------------------------------------------------


class TestCrossSignalCoherence:
    def test_bullish_trend_momentum_and_order_flow_agree(self) -> None:
        """En tendencia alcista, momentum y order_flow son ambos positivos."""
        for snap in _trending_up_series():
            mom = calculate_momentum(snap).signal
            ofi = calculate_order_flow_imbalance(snap).signal
            assert mom > 0 and ofi > 0, f"Desacuerdo: mom={mom:.3f}, ofi={ofi:.3f}"

    def test_bearish_trend_momentum_and_order_flow_agree(self) -> None:
        """En tendencia bajista, momentum y order_flow son ambos negativos."""
        for snap in _trending_down_series():
            mom = calculate_momentum(snap).signal
            ofi = calculate_order_flow_imbalance(snap).signal
            assert mom < 0 and ofi < 0, f"Desacuerdo: mom={mom:.3f}, ofi={ofi:.3f}"

    def test_breakout_bullish_outside_signal_is_positive(self) -> None:
        for snap in _breakout_bullish_series()[2:]:
            assert calculate_breakout(snap).signal > 0

    def test_breakout_bearish_outside_signal_is_negative(self) -> None:
        for snap in _breakout_bearish_series()[2:]:
            assert calculate_breakout(snap).signal < 0

    def test_sweep_signs_are_consistent_across_series(self) -> None:
        for snap in _sweep_bullish_series():
            assert calculate_liquidity_sweeps(snap).signal > 0
        for snap in _sweep_bearish_series():
            assert calculate_liquidity_sweeps(snap).signal < 0

    def test_mean_reversion_opposes_extended_price_moves(self) -> None:
        """Precio extendido al alza → mean_reversion negativa (espera corrección)."""
        base = 50000.0
        snap = _snapshot(
            last_price=base * 1.08,
            tf_5m=_candle(base, base),
            tf_15m=_candle(base * 0.99, base * 0.99),
            tf_1h=_candle(base * 0.98, base * 0.98),
            tf_4h=_candle(base * 0.97, base * 0.97),
        )
        assert compute_mean_reversion_signal(snap) < 0
