"""Tests unitarios para la Calculadora de Leverage Dinámico (F6, tarjeta [57])."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.core.config import Environment, LeverageConfig, LeverageMode
from backend.market_regime.schemas import MarketRegime
from backend.volatility.leverage import (
    _REGIME_MULTIPLIERS,
    _env_cap,
    compute_dynamic_leverage,
)
from backend.volatility.schemas import VolatilityAssessmentPackage, VolatilityRegime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_LEVERAGE_CONFIG = LeverageConfig(
    mode=LeverageMode.DYNAMIC_BY_VOLATILITY,
    max_leverage_paper=10,
    max_leverage_testnet=5,
    max_leverage_live_initial=3,
    max_leverage_live_absolute=5,
    volatility_adjustment_enabled=True,
    liquidation_distance_required=True,
)


def _vol_assessment(
    leverage_cap: int = 10,
    volatility_score: float = 0.1,
    volatility_regime: VolatilityRegime = VolatilityRegime.CONTRACTION,
) -> VolatilityAssessmentPackage:
    now = datetime.now(UTC)
    return VolatilityAssessmentPackage(
        snapshot_id=str(uuid.uuid4()),
        timestamp_utc=now,
        symbol="BTCUSDT",
        atr_5m=Decimal("100"),
        atr_15m=Decimal("150"),
        atr_1h=Decimal("200"),
        atr_4h=Decimal("300"),
        atr_percent=0.4,
        realized_vol=0.004,
        volatility_regime=volatility_regime,
        volatility_score=volatility_score,
        leverage_cap=leverage_cap,
        liquidation_risk_score=0.2,
        details={},
    )


# ---------------------------------------------------------------------------
# Tests de _env_cap
# ---------------------------------------------------------------------------


class TestEnvCap:
    def test_paper_cap(self) -> None:
        assert _env_cap(Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG) == 10

    def test_testnet_cap(self) -> None:
        assert _env_cap(Environment.TESTNET, _DEFAULT_LEVERAGE_CONFIG) == 5

    def test_live_uses_absolute_cap(self) -> None:
        assert _env_cap(Environment.LIVE, _DEFAULT_LEVERAGE_CONFIG) == 5


# ---------------------------------------------------------------------------
# Tests de multiplicadores por régimen
# ---------------------------------------------------------------------------


class TestRegimeMultipliers:
    def test_all_regimes_have_multiplier(self) -> None:
        for regime in MarketRegime:
            assert regime in _REGIME_MULTIPLIERS

    def test_multipliers_in_valid_range(self) -> None:
        for regime, mult in _REGIME_MULTIPLIERS.items():
            assert 0.0 < mult <= 1.0, f"{regime} multiplier {mult} out of range"

    def test_unclear_is_most_conservative(self) -> None:
        unclear_mult = _REGIME_MULTIPLIERS[MarketRegime.UNCLEAR]
        for regime, mult in _REGIME_MULTIPLIERS.items():
            if regime != MarketRegime.UNCLEAR:
                assert unclear_mult <= mult, f"UNCLEAR should be <= {regime}"


# ---------------------------------------------------------------------------
# Tests de compute_dynamic_leverage — topes de entorno
# ---------------------------------------------------------------------------


class TestEnvironmentCaps:
    def test_paper_never_exceeds_10x(self) -> None:
        # vol_cap = 10, TRENDING (×1.0) → debería ser 10, cap PAPER = 10
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage <= 10

    def test_testnet_never_exceeds_5x(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.TESTNET,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage <= 5

    def test_live_never_exceeds_5x_absolute(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.LIVE,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage <= 5

    def test_env_cap_stored_in_result(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.TESTNET,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.env_cap == 5


# ---------------------------------------------------------------------------
# Tests de compute_dynamic_leverage — ajuste por régimen
# ---------------------------------------------------------------------------


class TestRegimeAdjustment:
    def test_trending_keeps_vol_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.regime_adjusted == 10
        assert result.suggested_leverage == 10

    def test_unclear_halves_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.UNCLEAR,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        # floor(10 × 0.5) = 5
        assert result.regime_adjusted == 5
        assert result.suggested_leverage == 5

    def test_ranging_reduces_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.RANGING,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        # floor(10 × 0.8) = 8
        assert result.regime_adjusted == 8
        assert result.suggested_leverage == 8

    def test_high_vol_regime_reduces_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.HIGH_VOL,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        # floor(10 × 0.7) = 7
        assert result.regime_adjusted == 7
        assert result.suggested_leverage == 7

    def test_breakout_reduces_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.BREAKOUT,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        # floor(10 × 0.7) = 7
        assert result.regime_adjusted == 7
        assert result.suggested_leverage == 7

    def test_low_vol_keeps_vol_cap(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=7),
            MarketRegime.LOW_VOL,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        # floor(7 × 1.0) = 7
        assert result.regime_adjusted == 7
        assert result.suggested_leverage == 7

    def test_unclear_regime_always_most_conservative(self) -> None:
        """UNCLEAR debe dar el leverage más bajo para el mismo vol_cap y entorno."""
        vol = _vol_assessment(leverage_cap=10)
        unclear_result = compute_dynamic_leverage(
            vol, MarketRegime.UNCLEAR, Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG
        )
        for regime in MarketRegime:
            r = compute_dynamic_leverage(vol, regime, Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG)
            assert unclear_result.suggested_leverage <= r.suggested_leverage


# ---------------------------------------------------------------------------
# Tests de interacción entorno × régimen
# ---------------------------------------------------------------------------


class TestEnvironmentRegimeInteraction:
    def test_testnet_caps_trending_high_vol_cap(self) -> None:
        # TRENDING no reduce, pero TESTNET cap 5x debe aplicarse
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.TRENDING,
            Environment.TESTNET,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage == 5  # env_cap aplica

    def test_live_caps_low_vol_regime(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.LOW_VOL,
            Environment.LIVE,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage <= 5

    def test_regime_applies_before_env_cap(self) -> None:
        # vol_cap=4, UNCLEAR × 0.5 = floor(2) = 2 → env_cap TESTNET=5 → suggested=2
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=4),
            MarketRegime.UNCLEAR,
            Environment.TESTNET,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.regime_adjusted == 2
        assert result.suggested_leverage == 2


# ---------------------------------------------------------------------------
# Tests de valores límite
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_result_always_ge_1(self) -> None:
        for regime in MarketRegime:
            for env in Environment:
                result = compute_dynamic_leverage(
                    _vol_assessment(leverage_cap=1),
                    regime,
                    env,
                    _DEFAULT_LEVERAGE_CONFIG,
                )
                assert result.suggested_leverage >= 1

    def test_vol_cap_1_unclear_live_gives_1(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=1),
            MarketRegime.UNCLEAR,
            Environment.LIVE,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.suggested_leverage == 1

    def test_higher_vol_cap_never_gives_higher_leverage_same_regime_env(self) -> None:
        """Monotonía: mayor vol_cap → mayor o igual leverage sugerido."""
        regime = MarketRegime.TRENDING
        env = Environment.PAPER
        prev = 0
        for cap in (1, 3, 5, 7, 10):
            r = compute_dynamic_leverage(
                _vol_assessment(leverage_cap=cap), regime, env, _DEFAULT_LEVERAGE_CONFIG
            )
            assert r.suggested_leverage >= prev
            prev = r.suggested_leverage


# ---------------------------------------------------------------------------
# Tests de determinismo y estructura del output
# ---------------------------------------------------------------------------


class TestDeterminismAndOutput:
    def test_same_input_same_output(self) -> None:
        vol = _vol_assessment(leverage_cap=7)
        r1 = compute_dynamic_leverage(
            vol, MarketRegime.RANGING, Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG
        )
        r2 = compute_dynamic_leverage(
            vol, MarketRegime.RANGING, Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG
        )
        assert r1.suggested_leverage == r2.suggested_leverage
        assert r1.regime_adjusted == r2.regime_adjusted

    def test_output_stores_regime_and_environment(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=5),
            MarketRegime.BREAKOUT,
            Environment.TESTNET,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        assert result.regime == MarketRegime.BREAKOUT
        assert result.environment == Environment.TESTNET

    def test_details_contains_breakdown(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=10),
            MarketRegime.RANGING,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        required_keys = {
            "vol_cap",
            "regime_multiplier",
            "regime_adjusted",
            "env_cap",
            "assessment_id",
            "volatility_score",
            "volatility_regime",
        }
        assert required_keys <= set(result.details.keys())

    def test_vol_cap_in_output_matches_assessment(self) -> None:
        vol = _vol_assessment(leverage_cap=7)
        result = compute_dynamic_leverage(
            vol, MarketRegime.TRENDING, Environment.PAPER, _DEFAULT_LEVERAGE_CONFIG
        )
        assert result.vol_cap == 7

    def test_result_is_immutable(self) -> None:
        result = compute_dynamic_leverage(
            _vol_assessment(leverage_cap=5),
            MarketRegime.RANGING,
            Environment.PAPER,
            _DEFAULT_LEVERAGE_CONFIG,
        )
        with pytest.raises(ValidationError):
            result.suggested_leverage = 99
