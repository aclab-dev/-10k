"""Tests unitarios para el Volatility Engine (F6, tarjeta 55)."""

from __future__ import annotations

import uuid
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
from backend.volatility.engine import (
    _EXPANSION_THRESHOLD,
    _LEVERAGE_BANDS,
    _abs_roc,
    _atr_from_candle,
    _leverage_cap_for_score,
    _range_vol,
    compute_volatility_assessment,
)
from backend.volatility.schemas import VolatilityRegime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PRICE = Decimal("50000")
_BID = Decimal("49995")
_ASK = Decimal("50005")


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
    _default_high = max(d_open, d_close) * Decimal("1.001")
    _default_low = min(d_open, d_close) * Decimal("0.999")
    d_high = Decimal(str(high_price)) if high_price is not None else _default_high
    d_low = Decimal(str(low_price)) if low_price is not None else _default_low
    return CandleData(
        open=d_open,
        high=d_high,
        low=d_low,
        close=d_close,
        volume=Decimal("1000"),
        n_candles=n_candles,
    )


def _flat_candle(price: float = 50000.0) -> CandleData:
    """Vela completamente plana: H=L=open=close (sin rango)."""
    d = Decimal(str(price))
    epsilon = Decimal("0.01")
    return CandleData(
        open=d,
        high=d + epsilon,
        low=d - epsilon,
        close=d,
        volume=Decimal("500"),
        n_candles=10,
    )


def _volatile_candle(price: float = 50000.0, move_pct: float = 0.05) -> CandleData:
    """Vela con movimiento del move_pct% (alcista)."""
    return _candle(
        open_price=price,
        close_price=price * (1 + move_pct),
    )


def _snapshot(
    tf_5m: CandleData | None = None,
    tf_15m: CandleData | None = None,
    tf_1h: CandleData | None = None,
    tf_4h: CandleData | None = None,
    last_price: Decimal = _BASE_PRICE,
) -> MarketSnapshot:
    tf_5m = tf_5m or _flat_candle()
    tf_15m = tf_15m or _flat_candle()
    tf_1h = tf_1h or _flat_candle()
    tf_4h = tf_4h or _flat_candle()
    now = _now()
    bid = last_price - Decimal("5")
    ask = last_price + Decimal("5")
    spread_abs = ask - bid
    spread_pct = spread_abs / bid * 100
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=last_price,
        bid=bid,
        ask=ask,
        spread_absolute=spread_abs,
        spread_percent=spread_pct,
        candles=Candles(tf_5m=tf_5m, tf_15m=tf_15m, tf_1h=tf_1h, tf_4h=tf_4h),
        volume=Decimal("2000"),
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
# Tests de helpers internos
# ---------------------------------------------------------------------------


class TestAtrFromCandle:
    def test_returns_high_minus_low(self) -> None:
        candle = _candle(open_price=100.0, close_price=100.0, high_price=110.0, low_price=90.0)
        assert _atr_from_candle(candle) == Decimal("20")

    def test_zero_range_candle(self) -> None:
        candle = CandleData(
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("0"),
            n_candles=1,
        )
        assert _atr_from_candle(candle) == Decimal("0")


class TestAbsRoc:
    def test_bullish_candle(self) -> None:
        candle = _candle(open_price=100.0, close_price=110.0)
        assert abs(_abs_roc(candle) - 0.10) < 1e-9

    def test_bearish_candle(self) -> None:
        candle = _candle(open_price=100.0, close_price=90.0)
        assert abs(_abs_roc(candle) - 0.10) < 1e-9

    def test_flat_candle_returns_zero(self) -> None:
        candle = _candle(open_price=100.0, close_price=100.0)
        assert _abs_roc(candle) == 0.0

    def test_zero_open_does_not_raise(self) -> None:
        candle = CandleData(
            open=Decimal("0"),
            high=Decimal("100"),
            low=Decimal("0"),
            close=Decimal("0"),
            volume=Decimal("0"),
            n_candles=1,
        )
        assert _abs_roc(candle) == 0.0


class TestLeverageCapForScore:
    def test_low_vol_score_gives_highest_cap(self) -> None:
        assert _leverage_cap_for_score(0.0) == 10
        assert _leverage_cap_for_score(0.25) == 10

    def test_medium_low_vol(self) -> None:
        assert _leverage_cap_for_score(0.26) == 7
        assert _leverage_cap_for_score(0.50) == 7

    def test_medium_high_vol(self) -> None:
        assert _leverage_cap_for_score(0.51) == 5
        assert _leverage_cap_for_score(0.75) == 5

    def test_high_vol_gives_lowest_cap(self) -> None:
        assert _leverage_cap_for_score(0.76) == 3
        assert _leverage_cap_for_score(1.0) == 3


# ---------------------------------------------------------------------------
# Tests de compute_volatility_assessment
# ---------------------------------------------------------------------------


class TestVolatilityScoreBounds:
    def test_score_in_range_flat_market(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert 0.0 <= result.volatility_score <= 1.0

    def test_score_in_range_high_vol(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.10),
            tf_15m=_volatile_candle(move_pct=0.10),
            tf_1h=_volatile_candle(move_pct=0.10),
            tf_4h=_volatile_candle(move_pct=0.10),
        )
        result = compute_volatility_assessment(snap)
        assert 0.0 <= result.volatility_score <= 1.0

    def test_liquidation_risk_in_range_flat(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert 0.0 <= result.liquidation_risk_score <= 1.0

    def test_liquidation_risk_in_range_high_vol(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.15),
            tf_15m=_volatile_candle(move_pct=0.15),
            tf_1h=_volatile_candle(move_pct=0.15),
            tf_4h=_volatile_candle(move_pct=0.15),
        )
        result = compute_volatility_assessment(snap)
        assert 0.0 <= result.liquidation_risk_score <= 1.0


class TestVolatilityRegimeClassification:
    def test_flat_market_is_contraction(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert result.volatility_regime == VolatilityRegime.CONTRACTION

    def test_high_vol_market_is_expansion(self) -> None:
        # 10% de ROC intra-vela → score >> 0.5
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.10),
            tf_15m=_volatile_candle(move_pct=0.10),
            tf_1h=_volatile_candle(move_pct=0.10),
            tf_4h=_volatile_candle(move_pct=0.10),
        )
        result = compute_volatility_assessment(snap)
        assert result.volatility_regime == VolatilityRegime.EXPANSION

    def test_regime_consistent_with_score(self) -> None:
        for move_pct in (0.001, 0.005, 0.02, 0.05, 0.10):
            snap = _snapshot(
                tf_5m=_volatile_candle(move_pct=move_pct),
                tf_15m=_volatile_candle(move_pct=move_pct),
                tf_1h=_volatile_candle(move_pct=move_pct),
                tf_4h=_volatile_candle(move_pct=move_pct),
            )
            result = compute_volatility_assessment(snap)
            expected_regime = (
                VolatilityRegime.EXPANSION
                if result.volatility_score >= _EXPANSION_THRESHOLD
                else VolatilityRegime.CONTRACTION
            )
            assert result.volatility_regime == expected_regime


class TestLeverageCap:
    def test_flat_market_max_leverage(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert result.leverage_cap == 10

    def test_high_vol_market_min_leverage(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.20),
            tf_15m=_volatile_candle(move_pct=0.20),
            tf_1h=_volatile_candle(move_pct=0.20),
            tf_4h=_volatile_candle(move_pct=0.20),
        )
        result = compute_volatility_assessment(snap)
        assert result.leverage_cap == 3

    def test_leverage_cap_is_valid_value(self) -> None:
        valid_caps = {cap for _, cap in _LEVERAGE_BANDS}
        for move_pct in (0.0, 0.005, 0.01, 0.03, 0.05, 0.10, 0.20):
            snap = _snapshot(
                tf_5m=_volatile_candle(move_pct=move_pct),
                tf_15m=_volatile_candle(move_pct=move_pct),
                tf_1h=_volatile_candle(move_pct=move_pct),
                tf_4h=_volatile_candle(move_pct=move_pct),
            )
            result = compute_volatility_assessment(snap)
            assert result.leverage_cap in valid_caps

    def test_higher_vol_never_gives_higher_leverage_cap(self) -> None:
        low_vol = compute_volatility_assessment(_snapshot())
        high_vol = compute_volatility_assessment(
            _snapshot(
                tf_5m=_volatile_candle(move_pct=0.10),
                tf_15m=_volatile_candle(move_pct=0.10),
                tf_1h=_volatile_candle(move_pct=0.10),
                tf_4h=_volatile_candle(move_pct=0.10),
            )
        )
        assert high_vol.leverage_cap <= low_vol.leverage_cap


class TestAtrValues:
    def test_atr_matches_high_minus_low(self) -> None:
        candle_1h = _candle(
            open_price=49000.0,
            close_price=51000.0,
            high_price=52000.0,
            low_price=48000.0,
        )
        snap = _snapshot(tf_1h=candle_1h, last_price=Decimal("50995"))
        result = compute_volatility_assessment(snap)
        assert result.atr_1h == Decimal("4000")

    def test_atr_values_are_non_negative(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert result.atr_5m >= 0
        assert result.atr_15m >= 0
        assert result.atr_1h >= 0
        assert result.atr_4h >= 0

    def test_atr_percent_is_non_negative(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert result.atr_percent >= 0.0


class TestRangeVol:
    def test_full_range_captured(self) -> None:
        candle = _candle(open_price=100.0, close_price=100.0, high_price=110.0, low_price=90.0)
        assert abs(_range_vol(candle) - 0.20) < 1e-9

    def test_range_vol_gte_abs_roc(self) -> None:
        """range_vol siempre >= abs_roc porque H−L >= |close−open|."""
        for open_p, close_p, high_p, low_p in [
            (100.0, 110.0, 115.0, 95.0),
            (100.0, 90.0, 105.0, 88.0),
            (100.0, 100.0, 120.0, 80.0),  # flat body, wide range
        ]:
            c = _candle(open_price=open_p, close_price=close_p, high_price=high_p, low_price=low_p)
            assert _range_vol(c) >= _abs_roc(c)

    def test_flat_body_wide_range_captured(self) -> None:
        """Doji de mecha larga: body=0, rango=20%. Debe capturar volatilidad."""
        candle = _candle(open_price=100.0, close_price=100.0, high_price=110.0, low_price=90.0)
        assert _abs_roc(candle) == 0.0  # el cuerpo es plano...
        assert _range_vol(candle) == pytest.approx(0.20)  # ...pero el rango es alto

    def test_zero_open_does_not_raise(self) -> None:
        candle = CandleData(
            open=Decimal("0"),
            high=Decimal("100"),
            low=Decimal("0"),
            close=Decimal("50"),
            volume=Decimal("0"),
            n_candles=1,
        )
        assert _range_vol(candle) == 0.0


class TestRealizedVol:
    def test_flat_market_low_realized_vol(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        assert result.realized_vol >= 0.0
        # Mercado plano: ROC ≈ 0 → realized_vol muy bajo
        assert result.realized_vol < 0.001

    def test_high_move_higher_realized_vol(self) -> None:
        flat = compute_volatility_assessment(_snapshot())
        volatile = compute_volatility_assessment(
            _snapshot(
                tf_5m=_volatile_candle(move_pct=0.05),
                tf_15m=_volatile_candle(move_pct=0.05),
                tf_1h=_volatile_candle(move_pct=0.05),
                tf_4h=_volatile_candle(move_pct=0.05),
            )
        )
        assert volatile.realized_vol > flat.realized_vol


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.03),
            tf_15m=_volatile_candle(move_pct=0.02),
        )
        r1 = compute_volatility_assessment(snap)
        r2 = compute_volatility_assessment(snap)
        assert r1.volatility_score == r2.volatility_score
        assert r1.volatility_regime == r2.volatility_regime
        assert r1.leverage_cap == r2.leverage_cap
        assert r1.realized_vol == r2.realized_vol


class TestDbKwargs:
    def test_required_fields_present(self) -> None:
        snap = _snapshot()
        result = compute_volatility_assessment(snap)
        db = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))

        required_keys = {
            "id",
            "bot_run_id",
            "market_snapshot_id",
            "symbol",
            "timestamp",
            "atr",
            "atr_percent",
            "volatility_score",
            "leverage_cap",
            "liquidation_risk_score",
            "details",
        }
        assert required_keys <= set(db.keys())

    def test_atr_in_db_is_atr_1h(self) -> None:
        candle_1h = _candle(
            open_price=49000.0,
            close_price=51000.0,
            high_price=52000.0,
            low_price=48000.0,
        )
        snap = _snapshot(tf_1h=candle_1h, last_price=Decimal("50995"))
        result = compute_volatility_assessment(snap)
        db = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert db["atr"] == result.atr_1h

    def test_snapshot_id_linked(self) -> None:
        snap = _snapshot()
        result = compute_volatility_assessment(snap)
        db = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert db["market_snapshot_id"] == snap.snapshot_id

    def test_details_contains_per_tf_atr(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        db = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        details = db["details"]
        assert "atr_5m" in details
        assert "atr_15m" in details
        assert "atr_1h" in details
        assert "atr_4h" in details
        assert "realized_vol" in details
        assert "volatility_regime" in details

    def test_scores_within_bounds_in_db(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=0.05),
            tf_15m=_volatile_candle(move_pct=0.05),
            tf_1h=_volatile_candle(move_pct=0.05),
            tf_4h=_volatile_candle(move_pct=0.05),
        )
        result = compute_volatility_assessment(snap)
        db = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert 0.0 <= db["volatility_score"] <= 1.0
        assert 0.0 <= db["liquidation_risk_score"] <= 1.0


class TestUuidValidation:
    def test_assessment_id_is_valid_uuid(self) -> None:
        result = compute_volatility_assessment(_snapshot())
        uuid.UUID(result.assessment_id)  # no raise

    def test_snapshot_id_is_linked(self) -> None:
        snap = _snapshot()
        result = compute_volatility_assessment(snap)
        assert result.snapshot_id == snap.snapshot_id


class TestEdgeCases:
    def test_zero_open_candle_does_not_raise(self) -> None:
        zero_open = CandleData(
            open=Decimal("0"),
            high=Decimal("100"),
            low=Decimal("0"),
            close=Decimal("0"),
            volume=Decimal("500"),
            n_candles=1,
        )
        snap = _snapshot(tf_5m=zero_open)
        result = compute_volatility_assessment(snap)
        assert 0.0 <= result.volatility_score <= 1.0

    def test_very_large_move_caps_at_1(self) -> None:
        snap = _snapshot(
            tf_5m=_volatile_candle(move_pct=1.0),
            tf_15m=_volatile_candle(move_pct=1.0),
            tf_1h=_volatile_candle(move_pct=1.0),
            tf_4h=_volatile_candle(move_pct=1.0),
        )
        result = compute_volatility_assessment(snap)
        assert result.volatility_score <= 1.0
        assert result.liquidation_risk_score <= 1.0

    def test_wide_range_flat_body_is_expansion_with_conservative_leverage(self) -> None:
        """Caso Rodrigo: doji de mecha larga — body plano pero rango extremo.

        open=50000, high=60000, low=40000, close=50000 → rango 40%, body 0%.
        Con _abs_roc este caso daría CONTRACTION + leverage_cap 10x (bug).
        Con _range_vol debe dar EXPANSION + leverage_cap conservador.
        """
        doji = _candle(
            open_price=50000.0,
            close_price=50000.0,
            high_price=60000.0,
            low_price=40000.0,
        )
        snap = _snapshot(
            tf_5m=doji,
            tf_15m=doji,
            tf_1h=doji,
            tf_4h=doji,
            last_price=Decimal("50000"),
        )
        result = compute_volatility_assessment(snap)

        # Rango = 20000/50000 = 40% → realized_vol = 0.40 → score ≈ 1.0
        assert result.realized_vol == pytest.approx(0.40, rel=1e-3)
        assert result.volatility_score > 0.99
        assert result.volatility_regime == VolatilityRegime.EXPANSION
        assert result.leverage_cap == 3  # cap más conservador
