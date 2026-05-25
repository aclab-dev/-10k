"""Feature engineering pipeline for versioned model-ready features."""

from __future__ import annotations

from typing import Any, Final

from backend.feature_engineering.normalization import (
    enum_feature,
    optional_float,
    percent_feature,
    score_feature,
    signal_feature,
)
from backend.feature_engineering.schemas import FEATURE_PACKAGE_VERSION, FeatureEngineeringPackage
from backend.market_regime.schemas import MarketRegimeAssessment
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.volatility.schemas import VolatilityAssessmentPackage

# Maximum leverage cap produced by the volatility engine (highest band in engine.py).
# Used to normalize leverage_cap into [0, 1] for homogeneous feature vectors.
_MAX_LEVERAGE_CAP: Final[int] = 10


def build_feature_package(
    *,
    quant_signals: QuantSignalsPackage,
    market_regime: MarketRegimeAssessment,
    volatility: VolatilityAssessmentPackage,
) -> FeatureEngineeringPackage:
    """Build a deterministic feature package from F5/F6 upstream packages."""
    snapshot_id = quant_signals.snapshot_id
    if market_regime.snapshot_id != snapshot_id or volatility.snapshot_id != snapshot_id:
        raise ValueError("All inputs must belong to the same market snapshot")
    if market_regime.symbol != quant_signals.symbol or volatility.symbol != quant_signals.symbol:
        raise ValueError("All inputs must use the same symbol")

    features: dict[str, Any] = {
        # Quant signals, normalized to stable numeric ranges.
        "signal.momentum": signal_feature(quant_signals.momentum_signal),
        "signal.mean_reversion": signal_feature(quant_signals.mean_reversion_signal),
        "signal.breakout": signal_feature(quant_signals.breakout_signal),
        "signal.funding": signal_feature(quant_signals.funding_signal),
        "signal.open_interest": signal_feature(quant_signals.open_interest_signal),
        "signal.order_flow_imbalance": signal_feature(quant_signals.order_flow_imbalance_signal),
        "signal.liquidity_sweep": signal_feature(quant_signals.liquidity_sweep_signal),
        "signal.strength": score_feature(quant_signals.signal_strength_score),
        "signal.conflict": score_feature(quant_signals.signal_conflict_score),
        "signal.confidence": score_feature(quant_signals.signal_confidence),
        # Market regime classifications.
        "regime.primary": enum_feature(market_regime.primary_regime),
        "regime.secondary": enum_feature(market_regime.secondary_regime),
        "regime.confidence": score_feature(market_regime.regime_confidence),
        "regime.volatility_state": enum_feature(market_regime.volatility_state),
        "regime.liquidity_state": enum_feature(market_regime.liquidity_state),
        "regime.funding_state": enum_feature(market_regime.funding_state),
        "regime.open_interest_state": enum_feature(market_regime.open_interest_state),
        "regime.trend_alignment": enum_feature(market_regime.trend_alignment),
        # Volatility/risk features.
        "volatility.atr_percent_norm": percent_feature(volatility.atr_percent, cap=100.0),
        "volatility.realized_vol": optional_float(volatility.realized_vol),
        "volatility.regime": enum_feature(volatility.volatility_regime),
        "volatility.score": score_feature(volatility.volatility_score),
        "volatility.leverage_cap": score_feature(volatility.leverage_cap / _MAX_LEVERAGE_CAP),
        "volatility.liquidation_risk_score": score_feature(volatility.liquidation_risk_score),
    }

    source_versions = {
        "feature_engineering": FEATURE_PACKAGE_VERSION,
        "quant_signals": quant_signals.version,
        "market_regime": market_regime.version,
        "volatility": volatility.version,
    }
    details = {
        "input_ids": {
            "quant_signal_id": quant_signals.signal_id,
            "market_regime_id": market_regime.regime_id,
            "volatility_assessment_id": volatility.assessment_id,
        },
        "timeframes_used": list(quant_signals.timeframes_used),
    }

    return FeatureEngineeringPackage(
        snapshot_id=snapshot_id,
        symbol=quant_signals.symbol,
        timestamp_utc=quant_signals.timestamp_utc,
        version=FEATURE_PACKAGE_VERSION,
        features=features,
        source_versions=source_versions,
        details=details,
    )
