"""Unit tests for the F6 feature engineering layer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from backend.feature_engineering import (
    FEATURE_PACKAGE_VERSION,
    FeatureEngineeringPackage,
    FeatureStore,
    build_feature_package,
    compute_features_hash,
)
from backend.feature_engineering.normalization import (
    clamp,
    percent_feature,
    score_feature,
    signal_feature,
)
from backend.market_regime.schemas import (
    FundingState,
    LiquidityState,
    MarketRegimeAssessment,
    OpenInterestState,
    PrimaryRegime,
    TrendAlignment,
    VolatilityState,
)
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.storage.models import FeaturePackage
from backend.volatility.schemas import VolatilityAssessmentPackage, VolatilityRegime


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot_id() -> str:
    return str(uuid.uuid4())


def _quant(snapshot_id: str, **overrides: object) -> QuantSignalsPackage:
    defaults: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "timestamp_utc": _now(),
        "symbol": "BTCUSDT",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "momentum_signal": 0.4,
        "mean_reversion_signal": -0.2,
        "breakout_signal": 0.7,
        "funding_signal": 0.1,
        "open_interest_signal": 0.3,
        "order_flow_imbalance_signal": -0.5,
        "liquidity_sweep_signal": 0.0,
        "signal_strength_score": 0.8,
        "signal_conflict_score": 0.25,
        "signal_confidence": 0.9,
        "raw_feature_refs": {"momentum": {"method": "test"}},
    }
    defaults.update(overrides)
    return QuantSignalsPackage.model_validate(defaults)


def _regime(snapshot_id: str, **overrides: object) -> MarketRegimeAssessment:
    defaults: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "primary_regime": PrimaryRegime.TRENDING,
        "secondary_regime": PrimaryRegime.BREAKOUT,
        "regime_confidence": 0.85,
        "volatility_state": VolatilityState.NORMAL,
        "liquidity_state": LiquidityState.HIGH,
        "funding_state": FundingState.NEUTRAL,
        "open_interest_state": OpenInterestState.RISING,
        "trend_alignment": TrendAlignment.BULLISH,
        "notes": ["test"],
    }
    defaults.update(overrides)
    return MarketRegimeAssessment.model_validate(defaults)


def _volatility(snapshot_id: str, **overrides: object) -> VolatilityAssessmentPackage:
    defaults: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "timestamp_utc": _now(),
        "symbol": "BTCUSDT",
        "atr_5m": Decimal("100"),
        "atr_15m": Decimal("150"),
        "atr_1h": Decimal("200"),
        "atr_4h": Decimal("300"),
        "atr_percent": 0.4,
        "realized_vol": 0.004,
        "volatility_regime": VolatilityRegime.CONTRACTION,
        "volatility_score": 0.2,
        "leverage_cap": 7,
        "liquidation_risk_score": 0.3,
        "details": {"method": "test"},
    }
    defaults.update(overrides)
    return VolatilityAssessmentPackage.model_validate(defaults)


def _package(**overrides: object) -> FeatureEngineeringPackage:
    defaults: dict[str, Any] = {
        "snapshot_id": _snapshot_id(),
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "features": {"signal.momentum": 0.4, "regime.primary": "TRENDING"},
        "source_versions": {"feature_engineering": FEATURE_PACKAGE_VERSION},
    }
    defaults.update(overrides)
    return FeatureEngineeringPackage.model_validate(defaults)


class TestNormalization:
    def test_clamp_bounds_values(self) -> None:
        assert clamp(2.0, 0.0, 1.0) == 1.0
        assert clamp(-1.0, 0.0, 1.0) == 0.0

    def test_clamp_invalid_bounds_raises(self) -> None:
        with pytest.raises(ValueError, match="minimum cannot be greater than maximum"):
            clamp(0.5, 1.0, 0.0)

    def test_signal_feature_uses_neutral_for_none(self) -> None:
        assert signal_feature(None) == 0.0

    def test_signal_feature_clamps_to_range(self) -> None:
        assert signal_feature(2.0) == 1.0
        assert signal_feature(-2.0) == -1.0

    def test_score_feature_clamps_to_unit_interval(self) -> None:
        assert score_feature(1.5) == 1.0
        assert score_feature(-0.2) == 0.0

    def test_percent_feature_normalizes_by_cap(self) -> None:
        assert percent_feature(50.0, cap=100.0) == 0.5

    def test_percent_feature_invalid_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="cap must be positive"):
            percent_feature(0.5, cap=0.0)


class TestFeatureEngineeringPackage:
    def test_version_default_is_explicit(self) -> None:
        pkg = _package()
        assert pkg.version == FEATURE_PACKAGE_VERSION

    def test_package_id_is_uuid(self) -> None:
        pkg = _package()
        assert uuid.UUID(pkg.package_id)

    def test_invalid_snapshot_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid UUID"):
            _package(snapshot_id="not-a-uuid")

    def test_invalid_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not allowed"):
            _package(symbol="DOGEUSDT")

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTC"):
            _package(timestamp_utc=datetime.now())

    def test_non_utc_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTC"):
            _package(timestamp_utc=datetime.now(timezone(timedelta(hours=-3))))

    def test_result_is_immutable(self) -> None:
        pkg = _package()
        with pytest.raises(ValidationError):
            pkg.symbol = "ETHUSDT"

    def test_hash_is_sha256(self) -> None:
        pkg = _package()
        assert pkg.features_hash is not None
        assert len(pkg.features_hash) == 64

    def test_same_features_have_same_hash_despite_different_ids(self) -> None:
        snapshot_id = _snapshot_id()
        timestamp = _now()
        a = _package(snapshot_id=snapshot_id, timestamp_utc=timestamp)
        b = _package(snapshot_id=snapshot_id, timestamp_utc=timestamp)
        assert a.package_id != b.package_id
        assert a.features_hash == b.features_hash

    def test_hash_changes_when_feature_changes(self) -> None:
        a = _package(features={"x": 1.0})
        b = _package(features={"x": 2.0})
        assert a.features_hash != b.features_hash

    def test_hash_changes_when_version_changes(self) -> None:
        features = {"x": 1.0}
        assert compute_features_hash(features, version="1.0") != compute_features_hash(
            features, version="2.0"
        )

    def test_to_db_kwargs_matches_feature_packages_shape(self) -> None:
        pkg = _package()
        bot_run_id = str(uuid.uuid4())
        kwargs = pkg.to_db_kwargs(bot_run_id)
        assert kwargs["id"] == pkg.package_id
        assert kwargs["bot_run_id"] == bot_run_id
        assert kwargs["market_snapshot_id"] == pkg.snapshot_id
        assert kwargs["version"] == FEATURE_PACKAGE_VERSION
        assert kwargs["features_hash"] == pkg.features_hash
        assert kwargs["features"] == pkg.features
        # source_versions and details are intentionally excluded: no columns in DB model.
        assert "source_versions" not in kwargs
        assert "details" not in kwargs

    def test_external_hash_matching_content_is_accepted(self) -> None:
        pkg = _package()
        assert pkg.features_hash is not None
        pkg2 = _package(
            snapshot_id=pkg.snapshot_id,
            timestamp_utc=pkg.timestamp_utc,
            features=pkg.features,
            source_versions=pkg.source_versions,
            features_hash=pkg.features_hash,
        )
        assert pkg2.features_hash == pkg.features_hash

    def test_external_hash_mismatching_content_raises(self) -> None:
        valid_but_wrong_hash = "a" * 64
        with pytest.raises(ValueError, match="does not match the recomputed hash"):
            _package(features_hash=valid_but_wrong_hash)


class TestBuildFeaturePackage:
    def test_builds_expected_feature_keys(self) -> None:
        snapshot_id = _snapshot_id()
        pkg = build_feature_package(
            quant_signals=_quant(snapshot_id),
            market_regime=_regime(snapshot_id),
            volatility=_volatility(snapshot_id),
        )
        assert pkg.features["signal.momentum"] == 0.4
        assert pkg.features["regime.primary"] == "TRENDING"
        # leverage_cap=7, _MAX_LEVERAGE_CAP=10 → 7/10 = 0.7
        assert pkg.features["volatility.leverage_cap"] == pytest.approx(0.7, abs=1e-7)
        assert pkg.source_versions["feature_engineering"] == FEATURE_PACKAGE_VERSION

    def test_pipeline_is_reproducible_for_same_inputs(self) -> None:
        snapshot_id = _snapshot_id()
        quant = _quant(snapshot_id)
        regime = _regime(snapshot_id)
        volatility = _volatility(snapshot_id)
        a = build_feature_package(quant_signals=quant, market_regime=regime, volatility=volatility)
        b = build_feature_package(quant_signals=quant, market_regime=regime, volatility=volatility)
        assert a.features == b.features
        assert a.features_hash == b.features_hash

    def test_pipeline_hash_changes_when_input_changes(self) -> None:
        snapshot_id = _snapshot_id()
        a = build_feature_package(
            quant_signals=_quant(snapshot_id, momentum_signal=0.1),
            market_regime=_regime(snapshot_id),
            volatility=_volatility(snapshot_id),
        )
        b = build_feature_package(
            quant_signals=_quant(snapshot_id, momentum_signal=0.2),
            market_regime=_regime(snapshot_id),
            volatility=_volatility(snapshot_id),
        )
        assert a.features_hash != b.features_hash

    def test_pipeline_tolerates_optional_none_signals(self) -> None:
        snapshot_id = _snapshot_id()
        pkg = build_feature_package(
            quant_signals=_quant(
                snapshot_id,
                momentum_signal=None,
                signal_strength_score=None,
                signal_confidence=None,
            ),
            market_regime=_regime(snapshot_id),
            volatility=_volatility(snapshot_id),
        )
        assert pkg.features["signal.momentum"] == 0.0
        assert pkg.features["signal.strength"] == 0.0
        assert pkg.features["signal.confidence"] == 0.0

    def test_mismatched_snapshot_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="same market snapshot"):
            build_feature_package(
                quant_signals=_quant(_snapshot_id()),
                market_regime=_regime(_snapshot_id()),
                volatility=_volatility(_snapshot_id()),
            )

    def test_mismatched_symbol_rejected(self) -> None:
        snapshot_id = _snapshot_id()
        with pytest.raises(ValueError, match="same symbol"):
            build_feature_package(
                quant_signals=_quant(snapshot_id, symbol="BTCUSDT"),
                market_regime=_regime(snapshot_id, symbol="ETHUSDT"),
                volatility=_volatility(snapshot_id, symbol="BTCUSDT"),
            )


class TestFeatureStore:
    """Unit tests for FeatureStore — repository is mocked, no DB required."""

    def _make_store(self) -> tuple[FeatureStore, MagicMock]:
        """Return a FeatureStore wired to a mock repository and the mock itself."""
        mock_session = MagicMock()
        store = FeatureStore(mock_session)
        mock_repo = MagicMock()
        store._repo = mock_repo
        return store, mock_repo

    def test_save_delegates_to_repository(self) -> None:
        store, mock_repo = self._make_store()
        package = _package()
        bot_run_id = str(uuid.uuid4())

        db_record = MagicMock(spec=FeaturePackage)
        db_record.features_hash = package.features_hash
        db_record.id = package.package_id
        mock_repo.save.return_value = db_record

        result = store.save(package, bot_run_id=bot_run_id)

        mock_repo.save.assert_called_once()
        saved_kwargs = mock_repo.save.call_args[0][0]
        assert saved_kwargs.features_hash == package.features_hash
        assert result.features_hash == package.features_hash

    def test_get_by_hash_delegates_to_repository(self) -> None:
        store, mock_repo = self._make_store()
        package = _package()
        assert package.features_hash is not None

        db_record = MagicMock(spec=FeaturePackage)
        db_record.id = package.package_id
        mock_repo.get_by_hash.return_value = db_record

        result = store.get_by_hash(package.features_hash)

        mock_repo.get_by_hash.assert_called_once_with(package.features_hash)
        assert result is db_record

    def test_get_by_hash_returns_none_when_not_found(self) -> None:
        store, mock_repo = self._make_store()
        mock_repo.get_by_hash.return_value = None
        assert store.get_by_hash("a" * 64) is None

    def test_get_latest_by_symbol_delegates_to_repository(self) -> None:
        store, mock_repo = self._make_store()
        bot_run_id = str(uuid.uuid4())
        db_record = MagicMock(spec=FeaturePackage)
        mock_repo.get_latest_by_symbol.return_value = db_record

        result = store.get_latest_by_symbol(bot_run_id, "BTCUSDT")

        mock_repo.get_latest_by_symbol.assert_called_once_with(bot_run_id, "BTCUSDT")
        assert result is db_record
