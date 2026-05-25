"""Scenario-level pipeline tests for Epic F6 (Regime + Volatility + Features).

Each test feeds a realistic MarketSnapshot through the four F6 modules in sequence
and asserts that the outputs are correct and mutually consistent:

    MarketRegimeEngine → compute_volatility_assessment
                       → compute_dynamic_leverage
                       → build_feature_package

Scenarios:
  - Trending market: aligned bullish candles across all TFs → TRENDING regime
  - High volatility: extreme 1h range (>4%) → HIGH_VOLATILITY regime
  - Low volatility:  tight 1h range (<0.3%) → LOW_VOLATILITY regime
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from backend.core.config import Environment, LeverageConfig, LeverageMode
from backend.feature_engineering import build_feature_package
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.engine import MarketRegimeEngine
from backend.market_regime.schemas import PrimaryRegime, VolatilityState
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.volatility.engine import compute_volatility_assessment
from backend.volatility.leverage import compute_dynamic_leverage
from backend.volatility.schemas import VolatilityRegime

# ---------------------------------------------------------------------------
# Shared constants & helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_BASE_PRICE = Decimal("50000")

# Default spread produces last_price ≈ 50_010
_SPREAD_PCT = Decimal("0.04")
_SPREAD_ABS = _BASE_PRICE * _SPREAD_PCT / Decimal("100")  # 20
_BID = _BASE_PRICE
_ASK = _BID + _SPREAD_ABS
_LAST_PRICE = _BID + _SPREAD_ABS / Decimal("2")  # 50_010

_LEVERAGE_CFG = LeverageConfig(
    mode=LeverageMode.DYNAMIC_BY_VOLATILITY,
    max_leverage_paper=10,
    max_leverage_testnet=5,
    max_leverage_live_initial=3,
    max_leverage_live_absolute=5,
    volatility_adjustment_enabled=True,
    liquidation_distance_required=True,
)

_EXPECTED_FEATURE_KEYS = {
    "signal.momentum",
    "signal.mean_reversion",
    "signal.breakout",
    "signal.funding",
    "signal.open_interest",
    "signal.order_flow_imbalance",
    "signal.liquidity_sweep",
    "signal.strength",
    "signal.conflict",
    "signal.confidence",
    "regime.primary",
    "regime.secondary",
    "regime.confidence",
    "regime.volatility_state",
    "regime.liquidity_state",
    "regime.funding_state",
    "regime.open_interest_state",
    "regime.trend_alignment",
    "volatility.atr_percent_norm",
    "volatility.realized_vol",
    "volatility.regime",
    "volatility.score",
    "volatility.leverage_cap",
    "volatility.liquidation_risk_score",
}


def _candle(open: str, high: str, low: str, close: str) -> CandleData:
    return CandleData(
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("500"),
        n_candles=10,
    )


def _neutral_candle() -> CandleData:
    return _candle(open="49950", high="50100", low="49900", close="50000")


def _snapshot(
    candle_5m: CandleData | None = None,
    candle_15m: CandleData | None = None,
    candle_1h: CandleData | None = None,
    candle_4h: CandleData | None = None,
    snapshot_id: str | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=snapshot_id or str(uuid.uuid4()),
        timestamp_utc=_NOW,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol="BTCUSDT",
        last_price=_LAST_PRICE,
        bid=_BID,
        ask=_ASK,
        spread_absolute=_SPREAD_ABS,
        spread_percent=_SPREAD_PCT,
        candles=Candles(
            tf_5m=candle_5m or _neutral_candle(),
            tf_15m=candle_15m or _neutral_candle(),
            tf_1h=candle_1h or _neutral_candle(),
            tf_4h=candle_4h or _neutral_candle(),
        ),
        volume=Decimal("50000000"),
        funding_rate=0.0001,
        open_interest=Decimal("1000000"),
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


def _quant(snapshot_id: str, **overrides: Any) -> QuantSignalsPackage:
    defaults: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "timestamp_utc": _NOW,
        "symbol": "BTCUSDT",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "momentum_signal": 0.6,
        "mean_reversion_signal": -0.1,
        "breakout_signal": 0.3,
        "funding_signal": 0.1,
        "open_interest_signal": 0.2,
        "order_flow_imbalance_signal": 0.4,
        "liquidity_sweep_signal": 0.0,
        "signal_strength_score": 0.7,
        "signal_conflict_score": 0.2,
        "signal_confidence": 0.85,
        "raw_feature_refs": {},
    }
    defaults.update(overrides)
    return QuantSignalsPackage.model_validate(defaults)


# ---------------------------------------------------------------------------
# Scenario 1: Trending market
# Bullish candles aligned across all 4 TFs, moderate range (≈2% → HIGH realized_vol).
# Known outputs: TRENDING regime, ATR_1h = 1000, leverage_cap = 3 (high vol score).
# ---------------------------------------------------------------------------


class TestTrendingMarketPipeline:
    """Bullish alignment across all TFs → TRENDING regime, full pipeline consistent."""

    # Candles from test_market_regime_engine.py::test_trending_snapshot_produces_trending_regime
    _CANDLE_1H = _candle(open="49200", high="50000", low="49000", close="49800")
    _CANDLE_4H = _candle(open="49300", high="50000", low="49000", close="49800")
    _CANDLE_BULLISH = _candle(open="49200", high="50100", low="49100", close="49900")

    # ATR = high − low (exact, deterministic)
    _EXPECTED_ATR_1H = Decimal("1000")   # 50000 − 49000
    _EXPECTED_ATR_5M = Decimal("1000")   # 50100 − 49100

    # realized_vol ≈ mean(1000/49200, 1000/49200, 1000/49200, 1000/49300) ≈ 0.02032
    # volatility_score = tanh(0.02032 / 0.01) ≈ 0.966 → leverage_cap = 3
    _EXPECTED_LEVERAGE_CAP = 3
    _EXPECTED_VOL_REGIME = VolatilityRegime.EXPANSION

    # dynamic_leverage: TRENDING multiplier = 1.0 → floor(3 × 1.0) = 3, env_cap = 10
    _EXPECTED_DYNAMIC_LEVERAGE = 3

    @pytest.fixture
    def snapshot(self) -> MarketSnapshot:
        return _snapshot(
            candle_5m=self._CANDLE_BULLISH,
            candle_15m=self._CANDLE_BULLISH,
            candle_1h=self._CANDLE_1H,
            candle_4h=self._CANDLE_4H,
        )

    def test_regime_is_trending(self, snapshot: MarketSnapshot) -> None:
        result = MarketRegimeEngine().assess(snapshot)
        assert result.primary_regime == PrimaryRegime.TRENDING

    def test_atr_1h_matches_high_minus_low(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.atr_1h == self._EXPECTED_ATR_1H

    def test_atr_5m_matches_high_minus_low(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.atr_5m == self._EXPECTED_ATR_5M

    def test_leverage_cap(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.leverage_cap == self._EXPECTED_LEVERAGE_CAP

    def test_volatility_regime_is_expansion(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.volatility_regime == self._EXPECTED_VOL_REGIME

    def test_dynamic_leverage_for_paper(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.PAPER,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage == self._EXPECTED_DYNAMIC_LEVERAGE

    def test_dynamic_leverage_does_not_exceed_env_cap(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.PAPER,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage <= _LEVERAGE_CFG.max_leverage_paper

    def test_feature_package_has_all_keys(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        assert set(pkg.features.keys()) == _EXPECTED_FEATURE_KEYS

    def test_feature_package_snapshot_id_linked(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        assert pkg.snapshot_id == snapshot.snapshot_id

    def test_regime_feature_matches_assessment(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        assert pkg.features["regime.primary"] == PrimaryRegime.TRENDING.value


# ---------------------------------------------------------------------------
# Scenario 2: High volatility market
# Extreme 1h range (≈8.6%) triggers HIGH_VOLATILITY. Other TFs are neutral.
# Known outputs: HIGH_VOLATILITY regime, ATR_1h = 4200, leverage_cap = 3,
#                dynamic_leverage = 2 (HIGH_VOL multiplier 0.7 × cap 3 = 2.1 → 2).
# ---------------------------------------------------------------------------


class TestHighVolatilityPipeline:
    """Extreme 1h range → HIGH_VOLATILITY regime, conservative leverage."""

    # range_1h_pct = 4200 / 50010 ≈ 8.4% >> threshold 4%
    _CANDLE_HIGH_VOL_1H = _candle(open="49000", high="52200", low="48000", close="50000")

    _EXPECTED_ATR_1H = Decimal("4200")   # 52200 − 48000
    _EXPECTED_ATR_5M = Decimal("200")    # neutral: 50100 − 49900
    _EXPECTED_REGIME = PrimaryRegime.HIGH_VOLATILITY
    _EXPECTED_VOL_STATE = VolatilityState.HIGH
    _EXPECTED_LEVERAGE_CAP = 3
    _EXPECTED_VOL_REGIME = VolatilityRegime.EXPANSION

    # dynamic: HIGH_VOLATILITY multiplier = 0.7, floor(3 × 0.7) = 2
    _EXPECTED_DYNAMIC_LEVERAGE = 2

    @pytest.fixture
    def snapshot(self) -> MarketSnapshot:
        return _snapshot(candle_1h=self._CANDLE_HIGH_VOL_1H)

    def test_regime_is_high_volatility(self, snapshot: MarketSnapshot) -> None:
        result = MarketRegimeEngine().assess(snapshot)
        assert result.primary_regime == self._EXPECTED_REGIME

    def test_volatility_state_is_high(self, snapshot: MarketSnapshot) -> None:
        result = MarketRegimeEngine().assess(snapshot)
        assert result.volatility_state == self._EXPECTED_VOL_STATE

    def test_atr_1h_matches_extreme_range(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.atr_1h == self._EXPECTED_ATR_1H

    def test_atr_5m_matches_neutral_candle(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.atr_5m == self._EXPECTED_ATR_5M

    def test_leverage_cap_is_minimum(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.leverage_cap == self._EXPECTED_LEVERAGE_CAP

    def test_volatility_regime_is_expansion(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.volatility_regime == self._EXPECTED_VOL_REGIME

    def test_dynamic_leverage_reduced_by_high_vol_multiplier(
        self, snapshot: MarketSnapshot
    ) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.PAPER,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage == self._EXPECTED_DYNAMIC_LEVERAGE

    def test_dynamic_leverage_does_not_exceed_env_cap(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.PAPER,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage <= _LEVERAGE_CFG.max_leverage_paper

    def test_feature_package_has_all_keys(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        assert set(pkg.features.keys()) == _EXPECTED_FEATURE_KEYS

    def test_volatility_score_reflects_extreme_range(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        # With extreme range, score should be near 1.0 (tanh saturates)
        assert vol.volatility_score > 0.9

    def test_liquidation_risk_is_elevated(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.liquidation_risk_score > 0.5


# ---------------------------------------------------------------------------
# Scenario 3: Low volatility market
# Tight 1h range (≈0.2%) triggers LOW_VOLATILITY. All TFs use the same tight candle.
# Known outputs: LOW_VOLATILITY regime, ATR_1h = 100, leverage_cap = 10,
#                dynamic_leverage = 10 (LOW_VOL multiplier 1.0, env_cap = 10).
# ---------------------------------------------------------------------------


class TestLowVolatilityPipeline:
    """Tight range across all TFs → LOW_VOLATILITY regime, maximum leverage cap."""

    # range_1h_pct = 100 / 50010 ≈ 0.2% << threshold 0.3%
    _CANDLE_LOW_VOL = _candle(open="49960", high="50010", low="49910", close="49980")

    _EXPECTED_ATR_1H = Decimal("100")    # 50010 − 49910
    _EXPECTED_REGIME = PrimaryRegime.LOW_VOLATILITY
    _EXPECTED_VOL_STATE = VolatilityState.LOW
    _EXPECTED_LEVERAGE_CAP = 10
    _EXPECTED_VOL_REGIME = VolatilityRegime.CONTRACTION

    # dynamic: LOW_VOLATILITY multiplier = 1.0, floor(10 × 1.0) = 10, env_cap = 10
    _EXPECTED_DYNAMIC_LEVERAGE = 10

    @pytest.fixture
    def snapshot(self) -> MarketSnapshot:
        return _snapshot(
            candle_5m=self._CANDLE_LOW_VOL,
            candle_15m=self._CANDLE_LOW_VOL,
            candle_1h=self._CANDLE_LOW_VOL,
            candle_4h=self._CANDLE_LOW_VOL,
        )

    def test_regime_is_low_volatility(self, snapshot: MarketSnapshot) -> None:
        result = MarketRegimeEngine().assess(snapshot)
        assert result.primary_regime == self._EXPECTED_REGIME

    def test_volatility_state_is_low(self, snapshot: MarketSnapshot) -> None:
        result = MarketRegimeEngine().assess(snapshot)
        assert result.volatility_state == self._EXPECTED_VOL_STATE

    def test_atr_1h_matches_tight_range(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.atr_1h == self._EXPECTED_ATR_1H

    def test_leverage_cap_is_maximum(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.leverage_cap == self._EXPECTED_LEVERAGE_CAP

    def test_volatility_regime_is_contraction(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        assert vol.volatility_regime == self._EXPECTED_VOL_REGIME

    def test_dynamic_leverage_at_maximum_paper(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.PAPER,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage == self._EXPECTED_DYNAMIC_LEVERAGE

    def test_dynamic_leverage_testnet_capped(self, snapshot: MarketSnapshot) -> None:
        """TESTNET env_cap (5x) overrides LOW_VOL regime_adjusted (10x): min(10, 5) = 5."""
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.TESTNET,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage == _LEVERAGE_CFG.max_leverage_testnet

    def test_dynamic_leverage_live_capped(self, snapshot: MarketSnapshot) -> None:
        """LIVE env_cap (3x initial) overrides LOW_VOL regime_adjusted (10x): min(10, 3) = 3."""
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        lev = compute_dynamic_leverage(
            vol_assessment=vol,
            regime=regime.primary_regime,
            environment=Environment.LIVE,
            leverage_config=_LEVERAGE_CFG,
        )
        assert lev.suggested_leverage == _LEVERAGE_CFG.max_leverage_live_initial

    def test_feature_package_has_all_keys(self, snapshot: MarketSnapshot) -> None:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        assert set(pkg.features.keys()) == _EXPECTED_FEATURE_KEYS

    def test_volatility_score_is_low(self, snapshot: MarketSnapshot) -> None:
        vol = compute_volatility_assessment(snapshot)
        # With tight range, score should be well below EXPANSION threshold (0.5)
        assert vol.volatility_score < 0.5


# ---------------------------------------------------------------------------
# Pipeline determinism: same snapshot → same feature hash
# ---------------------------------------------------------------------------


class TestPipelineDeterminism:
    """Running the full F6 pipeline twice on the same snapshot produces the same hash."""

    def _run_pipeline(self, snapshot: MarketSnapshot) -> str:
        regime = MarketRegimeEngine().assess(snapshot)
        vol = compute_volatility_assessment(snapshot)
        pkg = build_feature_package(
            quant_signals=_quant(snapshot.snapshot_id),
            market_regime=regime,
            volatility=vol,
        )
        return pkg.features_hash

    def test_same_snapshot_same_hash(self) -> None:
        snap = _snapshot()
        h1 = self._run_pipeline(snap)
        h2 = self._run_pipeline(snap)
        assert h1 == h2

    def test_different_snapshots_different_hash(self) -> None:
        # Same snapshot_id to isolate: only candle data differs → hash must differ
        # because features (ATR, vol_score, regime) are computed from candle data.
        fixed_id = str(uuid.uuid4())
        snap_1h_high = _snapshot(
            candle_1h=_candle(open="49000", high="52200", low="48000", close="50000"),
            snapshot_id=fixed_id,
        )
        snap_1h_low = _snapshot(
            candle_1h=_candle(open="49960", high="50010", low="49910", close="49980"),
            snapshot_id=fixed_id,
        )
        h_high = self._run_pipeline(snap_1h_high)
        h_low = self._run_pipeline(snap_1h_low)
        assert h_high != h_low

    def test_hash_is_sha256_format(self) -> None:
        snap = _snapshot()
        h = self._run_pipeline(snap)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
