"""Tests unitarios del Market Regime Engine y sus clasificadores (sección 4.10.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.classifiers import (
    assess_funding_state,
    assess_liquidity_state,
    assess_open_interest_state,
    assess_trend_alignment,
    assess_volatility_state,
    classify_primary_regime,
    classify_secondary_regime,
)
from backend.market_regime.engine import MarketRegimeEngine
from backend.market_regime.schemas import (
    REGIME_VERSION,
    FundingState,
    LiquidityState,
    MarketRegimeAssessment,
    OpenInterestState,
    PrimaryRegime,
    TrendAlignment,
    VolatilityState,
)

# ---------------------------------------------------------------------------
# Helpers de construcción
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_BASE_PRICE = Decimal("50000")


def _candle(
    open: str,
    high: str,
    low: str,
    close: str,
    volume: str = "500",
) -> CandleData:
    return CandleData(
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        n_candles=10,
    )


def _neutral_candle() -> CandleData:
    """Vela sin señal clara: cuerpo pequeño, cierre en zona media, rango normal."""
    return _candle(open="49950", high="50100", low="49900", close="50000")


def _snapshot(
    candle_1h: CandleData | None = None,
    candle_4h: CandleData | None = None,
    candle_5m: CandleData | None = None,
    candle_15m: CandleData | None = None,
    spread_percent: str = "0.04",
    funding_rate: float | None = 0.0001,
    open_interest: str | None = "1000000",
) -> MarketSnapshot:
    """Construye un MarketSnapshot válido con overrides opcionales por timeframe."""
    spread_abs = Decimal("50000") * Decimal(spread_percent) / Decimal("100")
    bid = _BASE_PRICE
    ask = bid + spread_abs
    last_price = bid + spread_abs / 2

    return MarketSnapshot(
        snapshot_id=str(uuid.uuid4()),
        timestamp_utc=_NOW,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=last_price,
        bid=bid,
        ask=ask,
        spread_absolute=spread_abs,
        spread_percent=Decimal(spread_percent),
        candles=Candles(
            tf_5m=candle_5m or _neutral_candle(),
            tf_15m=candle_15m or _neutral_candle(),
            tf_1h=candle_1h or _neutral_candle(),
            tf_4h=candle_4h or _neutral_candle(),
        ),
        volume=Decimal("50000000"),
        funding_rate=funding_rate,
        open_interest=Decimal(open_interest) if open_interest else None,
        account_balance_usdt=Decimal("1000"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=_NOW,
        local_time=_NOW,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


# ---------------------------------------------------------------------------
# Tests de schema: MarketRegimeAssessment
# ---------------------------------------------------------------------------


class TestMarketRegimeAssessmentSchema:
    def test_minimal_construction(self) -> None:
        a = MarketRegimeAssessment(
            snapshot_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp_utc=_NOW,
            primary_regime=PrimaryRegime.UNCLEAR,
            regime_confidence=0.3,
            volatility_state=VolatilityState.NORMAL,
            liquidity_state=LiquidityState.NORMAL,
            funding_state=FundingState.NEUTRAL,
            open_interest_state=OpenInterestState.STABLE,
            trend_alignment=TrendAlignment.NEUTRAL,
        )
        assert a.primary_regime == PrimaryRegime.UNCLEAR
        assert a.secondary_regime is None
        assert a.notes == []
        assert a.version == REGIME_VERSION

    def test_regime_id_auto_generated(self) -> None:
        a = MarketRegimeAssessment(
            snapshot_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp_utc=_NOW,
            primary_regime=PrimaryRegime.TRENDING,
            regime_confidence=0.8,
            volatility_state=VolatilityState.NORMAL,
            liquidity_state=LiquidityState.NORMAL,
            funding_state=FundingState.NEUTRAL,
            open_interest_state=OpenInterestState.STABLE,
            trend_alignment=TrendAlignment.BULLISH,
        )
        assert uuid.UUID(a.regime_id)  # válido como UUID

    def test_two_instances_have_different_regime_ids(self) -> None:
        kwargs: dict = {
            "snapshot_id": str(uuid.uuid4()),
            "symbol": "BTCUSDT",
            "timestamp_utc": _NOW,
            "primary_regime": PrimaryRegime.TRENDING,
            "regime_confidence": 0.8,
            "volatility_state": VolatilityState.NORMAL,
            "liquidity_state": LiquidityState.NORMAL,
            "funding_state": FundingState.NEUTRAL,
            "open_interest_state": OpenInterestState.STABLE,
            "trend_alignment": TrendAlignment.BULLISH,
        }
        a = MarketRegimeAssessment(**kwargs)
        b = MarketRegimeAssessment(**kwargs)
        assert a.regime_id != b.regime_id

    def test_invalid_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            MarketRegimeAssessment(
                snapshot_id=str(uuid.uuid4()),
                symbol="DOGEUSDT",
                timestamp_utc=_NOW,
                primary_regime=PrimaryRegime.UNCLEAR,
                regime_confidence=0.3,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                funding_state=FundingState.NEUTRAL,
                open_interest_state=OpenInterestState.STABLE,
                trend_alignment=TrendAlignment.NEUTRAL,
            )

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MarketRegimeAssessment(
                snapshot_id=str(uuid.uuid4()),
                symbol="BTCUSDT",
                timestamp_utc=_NOW,
                primary_regime=PrimaryRegime.UNCLEAR,
                regime_confidence=1.5,  # > 1.0
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                funding_state=FundingState.NEUTRAL,
                open_interest_state=OpenInterestState.STABLE,
                trend_alignment=TrendAlignment.NEUTRAL,
            )

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTC"):
            MarketRegimeAssessment(
                snapshot_id=str(uuid.uuid4()),
                symbol="BTCUSDT",
                timestamp_utc=datetime(2024, 1, 1),  # naive, sin timezone
                primary_regime=PrimaryRegime.UNCLEAR,
                regime_confidence=0.3,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                funding_state=FundingState.NEUTRAL,
                open_interest_state=OpenInterestState.STABLE,
                trend_alignment=TrendAlignment.NEUTRAL,
            )

    def test_invalid_uuid_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            MarketRegimeAssessment(
                snapshot_id="not-a-uuid",
                symbol="BTCUSDT",
                timestamp_utc=_NOW,
                primary_regime=PrimaryRegime.UNCLEAR,
                regime_confidence=0.3,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                funding_state=FundingState.NEUTRAL,
                open_interest_state=OpenInterestState.STABLE,
                trend_alignment=TrendAlignment.NEUTRAL,
            )

    def test_to_db_kwargs_keys(self) -> None:
        snap_id = str(uuid.uuid4())
        a = MarketRegimeAssessment(
            snapshot_id=snap_id,
            symbol="ETHUSDT",
            timestamp_utc=_NOW,
            primary_regime=PrimaryRegime.TRENDING,
            secondary_regime=PrimaryRegime.HIGH_VOLATILITY,
            regime_confidence=0.75,
            volatility_state=VolatilityState.HIGH,
            liquidity_state=LiquidityState.NORMAL,
            funding_state=FundingState.POSITIVE,
            open_interest_state=OpenInterestState.STABLE,
            trend_alignment=TrendAlignment.BULLISH,
            notes=["test note"],
        )
        db = a.to_db_kwargs(bot_run_id="run-1")
        assert db["id"] == a.regime_id
        assert db["bot_run_id"] == "run-1"
        assert db["market_snapshot_id"] == snap_id
        assert db["symbol"] == "ETHUSDT"
        assert db["regime"] == PrimaryRegime.TRENDING
        assert db["confidence"] == 0.75
        details = db["regime_details"]
        assert details["secondary_regime"] == PrimaryRegime.HIGH_VOLATILITY
        assert details["volatility_state"] == VolatilityState.HIGH
        assert details["notes"] == ["test note"]
        assert details["version"] == REGIME_VERSION

    def test_immutable_after_construction(self) -> None:
        a = MarketRegimeAssessment(
            snapshot_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp_utc=_NOW,
            primary_regime=PrimaryRegime.UNCLEAR,
            regime_confidence=0.3,
            volatility_state=VolatilityState.NORMAL,
            liquidity_state=LiquidityState.NORMAL,
            funding_state=FundingState.NEUTRAL,
            open_interest_state=OpenInterestState.STABLE,
            trend_alignment=TrendAlignment.NEUTRAL,
        )
        with pytest.raises(ValidationError):
            a.primary_regime = PrimaryRegime.TRENDING  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests de assess_volatility_state
# ---------------------------------------------------------------------------


class TestAssessVolatilityState:
    def test_high_volatility_large_range(self) -> None:
        # range_1h > 4%: low=48000, high=52200 → range=4200, pct≈8.4%
        candle = _candle(open="49000", high="52200", low="48000", close="50000")
        snap = _snapshot(candle_1h=candle)
        assert assess_volatility_state(snap) == VolatilityState.HIGH

    def test_high_volatility_at_threshold(self) -> None:
        # range justo encima del umbral 4%: range=2002 sobre base ~50000 → 4.004%
        # low=49000, high=51002, open y close dentro del rango
        candle = _candle(open="49100", high="51002", low="49000", close="50001")
        snap = _snapshot(candle_1h=candle)
        assert assess_volatility_state(snap) == VolatilityState.HIGH

    def test_low_volatility_tiny_range(self) -> None:
        # range_1h < 0.3%: range=100 sobre base 50000 → 0.2%
        candle = _candle(open="49960", high="50010", low="49910", close="49980")
        snap = _snapshot(candle_1h=candle)
        assert assess_volatility_state(snap) == VolatilityState.LOW

    def test_normal_volatility_mid_range(self) -> None:
        # range_1h = 1%: range=500 sobre base 50000
        candle = _candle(open="49800", high="50250", low="49750", close="50000")
        snap = _snapshot(candle_1h=candle)
        assert assess_volatility_state(snap) == VolatilityState.NORMAL


# ---------------------------------------------------------------------------
# Tests de assess_liquidity_state
# ---------------------------------------------------------------------------


class TestAssessLiquidityState:
    def test_low_liquidity_high_spread(self) -> None:
        snap = _snapshot(spread_percent="2.0")
        assert assess_liquidity_state(snap) == LiquidityState.LOW

    def test_high_liquidity_low_spread(self) -> None:
        snap = _snapshot(spread_percent="0.02")
        assert assess_liquidity_state(snap) == LiquidityState.HIGH

    def test_normal_liquidity_mid_spread(self) -> None:
        snap = _snapshot(spread_percent="0.3")
        assert assess_liquidity_state(snap) == LiquidityState.NORMAL


# ---------------------------------------------------------------------------
# Tests de assess_funding_state
# ---------------------------------------------------------------------------


class TestAssessFundingState:
    def test_positive_funding(self) -> None:
        snap = _snapshot(funding_rate=0.0005)
        assert assess_funding_state(snap) == FundingState.POSITIVE

    def test_negative_funding(self) -> None:
        snap = _snapshot(funding_rate=-0.0005)
        assert assess_funding_state(snap) == FundingState.NEGATIVE

    def test_extreme_positive_funding(self) -> None:
        snap = _snapshot(funding_rate=0.003)
        assert assess_funding_state(snap) == FundingState.EXTREME

    def test_extreme_negative_funding(self) -> None:
        snap = _snapshot(funding_rate=-0.003)
        assert assess_funding_state(snap) == FundingState.EXTREME

    def test_neutral_when_funding_none(self) -> None:
        snap = _snapshot(funding_rate=None)
        assert assess_funding_state(snap) == FundingState.NEUTRAL

    def test_neutral_near_zero(self) -> None:
        snap = _snapshot(funding_rate=0.00005)
        assert assess_funding_state(snap) == FundingState.NEUTRAL


# ---------------------------------------------------------------------------
# Tests de assess_open_interest_state
# ---------------------------------------------------------------------------


class TestAssessOpenInterestState:
    def test_stable_when_oi_present(self) -> None:
        snap = _snapshot(open_interest="5000000")
        assert assess_open_interest_state(snap) == OpenInterestState.STABLE

    def test_unknown_when_oi_none(self) -> None:
        snap = _snapshot(open_interest=None)
        assert assess_open_interest_state(snap) == OpenInterestState.UNKNOWN


# ---------------------------------------------------------------------------
# Tests de assess_trend_alignment
# ---------------------------------------------------------------------------


class TestAssessTrendAlignment:
    def test_bullish_alignment_all_four_timeframes(self) -> None:
        # Cierre en top del rango en todos los TF (close_pos >= 0.70)
        bullish = _candle(open="49200", high="50100", low="49100", close="49900")
        snap = _snapshot(
            candle_5m=bullish,
            candle_15m=bullish,
            candle_1h=bullish,
            candle_4h=bullish,
        )
        assert assess_trend_alignment(snap) == TrendAlignment.BULLISH

    def test_bearish_alignment_all_four_timeframes(self) -> None:
        # Cierre en bottom del rango (close_pos <= 0.30)
        bearish = _candle(open="50800", high="51000", low="49800", close="50000")
        snap = _snapshot(
            candle_5m=bearish,
            candle_15m=bearish,
            candle_1h=bearish,
            candle_4h=bearish,
        )
        assert assess_trend_alignment(snap) == TrendAlignment.BEARISH

    def test_mixed_when_bullish_and_bearish_present(self) -> None:
        bullish = _candle(open="49200", high="50100", low="49100", close="49900")
        bearish = _candle(open="50800", high="51000", low="49800", close="50000")
        snap = _snapshot(
            candle_5m=bullish,
            candle_15m=bearish,
            candle_1h=bullish,
            candle_4h=bearish,
        )
        assert assess_trend_alignment(snap) == TrendAlignment.MIXED

    def test_neutral_when_closes_mid_range(self) -> None:
        # Cierres en zona media (ni bullish ni bearish)
        mid = _candle(open="49950", high="50100", low="49900", close="50000")
        snap = _snapshot(
            candle_5m=mid,
            candle_15m=mid,
            candle_1h=mid,
            candle_4h=mid,
        )
        assert assess_trend_alignment(snap) == TrendAlignment.NEUTRAL

    def test_bullish_with_three_of_four(self) -> None:
        bullish = _candle(open="49200", high="50100", low="49100", close="49900")
        mid = _candle(open="49950", high="50100", low="49900", close="50000")
        snap = _snapshot(
            candle_5m=bullish,
            candle_15m=bullish,
            candle_1h=bullish,
            candle_4h=mid,  # neutral, no bullish ni bearish
        )
        assert assess_trend_alignment(snap) == TrendAlignment.BULLISH


# ---------------------------------------------------------------------------
# Tests de classify_primary_regime
# ---------------------------------------------------------------------------


class TestClassifyPrimaryRegime:
    def test_high_volatility_regime(self) -> None:
        # range_1h ≈ 8% → HIGH_VOLATILITY
        candle = _candle(open="49000", high="52200", low="48000", close="50000")
        snap = _snapshot(candle_1h=candle)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, notes = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.HIGH_VOLATILITY
        assert 0.6 <= confidence <= 1.0
        assert any("range_1h_pct" in n for n in notes)

    def test_low_volatility_regime(self) -> None:
        # range_1h ≈ 0.2% → LOW_VOLATILITY
        candle = _candle(open="49960", high="50010", low="49910", close="49980")
        snap = _snapshot(candle_1h=candle)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, notes = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.LOW_VOLATILITY
        assert 0.6 <= confidence <= 1.0

    def test_breakout_regime_bullish(self) -> None:
        # body_ratio 1h ≈ 0.80, close en top, range ≈ 2.5%
        # low=48750, high=50000: range=1250 (2.5% de 50000)
        # close=49950: close_pos=(49950-48750)/1250=0.96 → bullish ✓
        # open=48950: body=|49950-48950|=1000, body_ratio=1000/1250=0.80 ✓
        candle = _candle(open="48950", high="50000", low="48750", close="49950")
        neutral_4h = _candle(open="49950", high="50100", low="49900", close="50000")
        snap = _snapshot(candle_1h=candle, candle_4h=neutral_4h)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, _ = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.BREAKOUT
        assert confidence >= 0.9

    def test_breakout_regime_bearish(self) -> None:
        # body_ratio 1h ≈ 0.80, close en bottom, range ≈ 2.5%
        # low=49000, high=50250: range=1250
        # close=49050: close_pos=(49050-49000)/1250=0.04 → bearish ✓
        # open=50050: body=|49050-50050|=1000, body_ratio=0.80 ✓
        candle = _candle(open="50050", high="50250", low="49000", close="49050")
        neutral_4h = _neutral_candle()
        snap = _snapshot(candle_1h=candle, candle_4h=neutral_4h)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, _, _ = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.BREAKOUT

    def test_trending_regime_bullish(self) -> None:
        # 1h: body_ratio=0.60, bullish close; 4h: body_ratio=0.50, bullish close
        # 1h: low=49000, high=50000, range=1000 (2%)
        #   body=600, bullish: open=49200, close=49800
        #   close_pos=(49800-49000)/1000=0.80 → bullish ✓
        # 4h: body_ratio=0.50, bullish
        #   low=49000, high=50000, body=500, open=49300, close=49800
        candle_1h = _candle(open="49200", high="50000", low="49000", close="49800")
        candle_4h = _candle(open="49300", high="50000", low="49000", close="49800")
        # También necesitamos que al menos 3 TF sean bullish para trend_alignment
        bullish_5m = _candle(open="49200", high="50100", low="49100", close="49900")
        bullish_15m = _candle(open="49200", high="50100", low="49100", close="49900")
        snap = _snapshot(
            candle_1h=candle_1h,
            candle_4h=candle_4h,
            candle_5m=bullish_5m,
            candle_15m=bullish_15m,
        )
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, _ = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.TRENDING
        assert confidence >= 0.5

    def test_ranging_regime(self) -> None:
        # 1h: body_ratio < 0.35, close en zona media (0.30 < close_pos < 0.70)
        # low=49800, high=50200, range=400 (0.8%)
        # close=50000: close_pos=(50000-49800)/400=0.50 → mid ✓
        # body=100: body_ratio=0.25 ✓
        # open=49900, close=50000 (bullish pequeño)
        candle = _candle(open="49900", high="50200", low="49800", close="50000")
        snap = _snapshot(candle_1h=candle)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, _ = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.RANGING
        assert confidence >= 0.5

    def test_unclear_fallback(self) -> None:
        # body_ratio en zona ambigua (0.35-0.50), close no extremo, sin alineación
        # 1h: low=49700, high=50300, range=600 (1.2%)
        # close=50100: close_pos=(50100-49700)/600=0.67 → justo bajo bullish threshold
        # open=49850, body=250, body_ratio=0.42 → entre WEAK y STRONG
        candle_1h = _candle(open="49850", high="50300", low="49700", close="50100")
        snap = _snapshot(candle_1h=candle_1h)
        vol_state = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        regime, confidence, notes = classify_primary_regime(snap, vol_state, trend)
        assert regime == PrimaryRegime.UNCLEAR
        assert confidence == 0.3
        assert len(notes) > 0

    def test_confidence_always_in_valid_range(self) -> None:
        candles_cases = [
            _candle(open="49000", high="52200", low="48000", close="50000"),  # HIGH_VOL
            _candle(open="49960", high="50010", low="49910", close="49980"),  # LOW_VOL
            _candle(open="49950", high="50100", low="49900", close="50000"),  # neutral
        ]
        for c in candles_cases:
            snap = _snapshot(candle_1h=c)
            vol = assess_volatility_state(snap)
            trend = assess_trend_alignment(snap)
            _, confidence, _ = classify_primary_regime(snap, vol, trend)
            assert 0.0 <= confidence <= 1.0, f"confidence={confidence} fuera de rango"

    def test_notes_always_non_empty(self) -> None:
        snap = _snapshot()
        vol = assess_volatility_state(snap)
        trend = assess_trend_alignment(snap)
        _, _, notes = classify_primary_regime(snap, vol, trend)
        assert len(notes) >= 1


# ---------------------------------------------------------------------------
# Tests de classify_secondary_regime
# ---------------------------------------------------------------------------


class TestClassifySecondaryRegime:
    def test_breakout_secondary_is_trending(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.BREAKOUT, VolatilityState.NORMAL, TrendAlignment.BULLISH
        )
        assert secondary == PrimaryRegime.TRENDING

    def test_trending_with_high_vol_secondary(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.TRENDING, VolatilityState.HIGH, TrendAlignment.BULLISH
        )
        assert secondary == PrimaryRegime.HIGH_VOLATILITY

    def test_ranging_with_low_vol_secondary(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.RANGING, VolatilityState.LOW, TrendAlignment.NEUTRAL
        )
        assert secondary == PrimaryRegime.LOW_VOLATILITY

    def test_unclear_with_aligned_trend_secondary(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.UNCLEAR, VolatilityState.NORMAL, TrendAlignment.BULLISH
        )
        assert secondary == PrimaryRegime.TRENDING

    def test_none_when_no_secondary_condition(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.TRENDING, VolatilityState.NORMAL, TrendAlignment.BULLISH
        )
        assert secondary is None

    def test_high_volatility_primary_has_no_secondary(self) -> None:
        secondary = classify_secondary_regime(
            PrimaryRegime.HIGH_VOLATILITY, VolatilityState.HIGH, TrendAlignment.MIXED
        )
        assert secondary is None


# ---------------------------------------------------------------------------
# Tests de integración: MarketRegimeEngine
# ---------------------------------------------------------------------------


class TestMarketRegimeEngine:
    def setup_method(self) -> None:
        self.engine = MarketRegimeEngine()

    def test_returns_market_regime_assessment(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        assert isinstance(result, MarketRegimeAssessment)

    def test_snapshot_id_propagated(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        assert result.snapshot_id == snap.snapshot_id

    def test_symbol_propagated(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        assert result.symbol == snap.symbol

    def test_timestamp_propagated(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        assert result.timestamp_utc == snap.timestamp_utc

    def test_high_vol_snapshot_produces_high_vol_regime(self) -> None:
        candle = _candle(open="49000", high="52200", low="48000", close="50000")
        snap = _snapshot(candle_1h=candle)
        result = self.engine.assess(snap)
        assert result.primary_regime == PrimaryRegime.HIGH_VOLATILITY
        assert result.volatility_state == VolatilityState.HIGH

    def test_low_vol_snapshot_produces_low_vol_regime(self) -> None:
        candle = _candle(open="49960", high="50010", low="49910", close="49980")
        snap = _snapshot(candle_1h=candle)
        result = self.engine.assess(snap)
        assert result.primary_regime == PrimaryRegime.LOW_VOLATILITY
        assert result.volatility_state == VolatilityState.LOW

    def test_trending_snapshot_produces_trending_regime(self) -> None:
        candle_1h = _candle(open="49200", high="50000", low="49000", close="49800")
        candle_4h = _candle(open="49300", high="50000", low="49000", close="49800")
        bullish = _candle(open="49200", high="50100", low="49100", close="49900")
        snap = _snapshot(
            candle_1h=candle_1h,
            candle_4h=candle_4h,
            candle_5m=bullish,
            candle_15m=bullish,
        )
        result = self.engine.assess(snap)
        assert result.primary_regime == PrimaryRegime.TRENDING

    def test_extreme_funding_detected(self) -> None:
        snap = _snapshot(funding_rate=0.005)
        result = self.engine.assess(snap)
        assert result.funding_state == FundingState.EXTREME

    def test_no_open_interest_produces_unknown_state(self) -> None:
        snap = _snapshot(open_interest=None)
        result = self.engine.assess(snap)
        assert result.open_interest_state == OpenInterestState.UNKNOWN

    def test_high_spread_produces_low_liquidity(self) -> None:
        snap = _snapshot(spread_percent="2.0")
        result = self.engine.assess(snap)
        assert result.liquidity_state == LiquidityState.LOW

    def test_engine_is_deterministic(self) -> None:
        snap = _snapshot()
        r1 = self.engine.assess(snap)
        r2 = self.engine.assess(snap)
        # Mismo snapshot → mismo régimen (excepto regime_id que siempre es nuevo)
        assert r1.primary_regime == r2.primary_regime
        assert r1.regime_confidence == r2.regime_confidence
        assert r1.volatility_state == r2.volatility_state
        assert r1.trend_alignment == r2.trend_alignment

    def test_result_has_notes(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        assert isinstance(result.notes, list)
        assert len(result.notes) >= 1

    def test_confidence_always_in_range(self) -> None:
        for spread in ["0.02", "0.3", "2.0"]:
            snap = _snapshot(spread_percent=spread)
            result = self.engine.assess(snap)
            assert 0.0 <= result.regime_confidence <= 1.0

    def test_to_db_kwargs_from_engine_result(self) -> None:
        snap = _snapshot()
        result = self.engine.assess(snap)
        db = result.to_db_kwargs(bot_run_id="test-run")
        required_keys = {
            "id",
            "bot_run_id",
            "market_snapshot_id",
            "symbol",
            "timestamp",
            "regime",
            "confidence",
            "regime_details",
        }
        assert required_keys <= set(db.keys())

    def test_all_six_regimes_are_reachable(self) -> None:
        """Verifica que el engine puede producir los 6 regímenes posibles."""
        observed: set[PrimaryRegime] = set()

        # HIGH_VOLATILITY
        c = _candle(open="49000", high="52200", low="48000", close="50000")
        observed.add(self.engine.assess(_snapshot(candle_1h=c)).primary_regime)

        # LOW_VOLATILITY
        c = _candle(open="49960", high="50010", low="49910", close="49980")
        observed.add(self.engine.assess(_snapshot(candle_1h=c)).primary_regime)

        # BREAKOUT (bullish)
        c = _candle(open="48950", high="50000", low="48750", close="49950")
        observed.add(self.engine.assess(_snapshot(candle_1h=c)).primary_regime)

        # RANGING
        c = _candle(open="49900", high="50200", low="49800", close="50000")
        observed.add(self.engine.assess(_snapshot(candle_1h=c)).primary_regime)

        # TRENDING
        c_1h = _candle(open="49200", high="50000", low="49000", close="49800")
        c_4h = _candle(open="49300", high="50000", low="49000", close="49800")
        bull = _candle(open="49200", high="50100", low="49100", close="49900")
        observed.add(
            self.engine.assess(
                _snapshot(candle_1h=c_1h, candle_4h=c_4h, candle_5m=bull, candle_15m=bull)
            ).primary_regime
        )

        # UNCLEAR
        c = _candle(open="49850", high="50300", low="49700", close="50100")
        observed.add(self.engine.assess(_snapshot(candle_1h=c)).primary_regime)

        assert PrimaryRegime.HIGH_VOLATILITY in observed
        assert PrimaryRegime.LOW_VOLATILITY in observed
        assert PrimaryRegime.BREAKOUT in observed
        assert PrimaryRegime.RANGING in observed
        assert PrimaryRegime.TRENDING in observed
        assert PrimaryRegime.UNCLEAR in observed
