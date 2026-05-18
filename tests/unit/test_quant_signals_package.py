"""Tests unitarios para el contrato QuantSignalsPackage (sección 4.10.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.quant_signals.schemas import SIGNAL_VERSION, QuantSignalsPackage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _package(**overrides: object) -> QuantSignalsPackage:
    """Construye un QuantSignalsPackage válido con valores mínimos."""
    defaults: dict[str, object] = {
        "snapshot_id": str(uuid.uuid4()),
        "timestamp_utc": _now(),
        "symbol": "BTCUSDT",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
    }
    defaults.update(overrides)
    return QuantSignalsPackage(**defaults)


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestQuantSignalsPackageConstruction:
    def test_minimal_fields_accepted(self) -> None:
        pkg = _package()
        assert pkg.symbol == "BTCUSDT"
        assert pkg.version == SIGNAL_VERSION

    def test_signal_id_auto_generated(self) -> None:
        pkg = _package()
        assert uuid.UUID(pkg.signal_id)  # válido como UUID

    def test_two_instances_have_different_signal_ids(self) -> None:
        a = _package()
        b = _package()
        assert a.signal_id != b.signal_id

    def test_explicit_signal_id_preserved(self) -> None:
        fixed_id = str(uuid.uuid4())
        pkg = _package(signal_id=fixed_id)
        assert pkg.signal_id == fixed_id

    def test_all_optional_signals_none_by_default(self) -> None:
        pkg = _package()
        assert pkg.momentum_signal is None
        assert pkg.mean_reversion_signal is None
        assert pkg.breakout_signal is None
        assert pkg.funding_signal is None
        assert pkg.open_interest_signal is None
        assert pkg.order_flow_imbalance_signal is None
        assert pkg.liquidity_sweep_signal is None
        assert pkg.signal_strength_score is None
        assert pkg.signal_conflict_score is None
        assert pkg.signal_confidence is None
        assert pkg.raw_feature_refs is None

    def test_full_package_accepted(self) -> None:
        pkg = _package(
            momentum_signal=0.8,
            mean_reversion_signal=-0.3,
            breakout_signal=0.5,
            funding_signal=-0.1,
            open_interest_signal=0.2,
            order_flow_imbalance_signal=0.6,
            liquidity_sweep_signal=-0.4,
            signal_strength_score=0.7,
            signal_conflict_score=0.2,
            signal_confidence=0.85,
            raw_feature_refs={"rsi_14": 62.5},
        )
        assert pkg.momentum_signal == 0.8
        assert pkg.signal_confidence == 0.85

    def test_version_default(self) -> None:
        pkg = _package()
        assert pkg.version == "1.0"

    def test_custom_version_accepted(self) -> None:
        pkg = _package(version="2.0")
        assert pkg.version == "2.0"


# ---------------------------------------------------------------------------
# Validación de UUID
# ---------------------------------------------------------------------------


class TestUUIDValidation:
    def test_valid_uuid_signal_id_accepted(self) -> None:
        valid_id = str(uuid.uuid4())
        pkg = _package(signal_id=valid_id)
        assert pkg.signal_id == valid_id

    def test_invalid_signal_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID válido"):
            _package(signal_id="not-a-uuid")

    def test_invalid_snapshot_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID válido"):
            _package(snapshot_id="not-a-uuid")

    def test_valid_uuid_snapshot_id_accepted(self) -> None:
        valid_id = str(uuid.uuid4())
        pkg = _package(snapshot_id=valid_id)
        assert pkg.snapshot_id == valid_id


# ---------------------------------------------------------------------------
# Validación de symbol
# ---------------------------------------------------------------------------


class TestSymbolValidation:
    @pytest.mark.parametrize(
        "symbol",
        ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    )
    def test_valid_symbols_accepted(self, symbol: str) -> None:
        pkg = _package(symbol=symbol)
        assert pkg.symbol == symbol

    @pytest.mark.parametrize("symbol", ["DOGEUSDT", "BTCUSD", "BTC", "", "btcusdt"])
    def test_invalid_symbol_rejected(self, symbol: str) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            _package(symbol=symbol)


# ---------------------------------------------------------------------------
# Validación de timestamp
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    def test_utc_aware_datetime_accepted(self) -> None:
        pkg = _package(timestamp_utc=datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        assert pkg.timestamp_utc.tzinfo is not None

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTC"):
            _package(timestamp_utc=datetime(2024, 1, 1, 12, 0))

    def test_non_utc_timezone_rejected(self) -> None:
        # El campo se llama timestamp_utc — el contrato garantiza offset=0
        tz_plus3 = timezone(timedelta(hours=3))
        with pytest.raises(ValidationError, match="UTC"):
            _package(timestamp_utc=datetime(2024, 1, 1, 12, 0, tzinfo=tz_plus3))

    def test_utc_plus_zero_explicit_accepted(self) -> None:
        tz_utc = timezone(timedelta(0))
        pkg = _package(timestamp_utc=datetime(2024, 1, 1, 12, 0, tzinfo=tz_utc))
        assert pkg.timestamp_utc.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# Validación de timeframes_used
# ---------------------------------------------------------------------------


class TestTimeframesValidation:
    def test_all_valid_timeframes_accepted(self) -> None:
        pkg = _package(timeframes_used=["5m", "15m", "1h", "4h"])
        assert len(pkg.timeframes_used) == 4

    def test_subset_of_timeframes_accepted(self) -> None:
        pkg = _package(timeframes_used=["1h", "4h"])
        assert pkg.timeframes_used == ["1h", "4h"]

    def test_single_timeframe_accepted(self) -> None:
        pkg = _package(timeframes_used=["5m"])
        assert pkg.timeframes_used == ["5m"]

    def test_invalid_timeframe_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Timeframes inválidos"):
            _package(timeframes_used=["5m", "30m"])

    def test_empty_timeframes_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _package(timeframes_used=[])

    def test_completely_invalid_timeframes_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Timeframes inválidos"):
            _package(timeframes_used=["1d", "1w"])


# ---------------------------------------------------------------------------
# Validación de señales individuales [-1.0, 1.0]
# ---------------------------------------------------------------------------


class TestIndividualSignalRangeValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "momentum_signal",
            "mean_reversion_signal",
            "breakout_signal",
            "funding_signal",
            "open_interest_signal",
            "order_flow_imbalance_signal",
            "liquidity_sweep_signal",
        ],
    )
    def test_boundary_values_accepted(self, field: str) -> None:
        for val in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            pkg = _package(**{field: val})
            assert getattr(pkg, field) == val

    @pytest.mark.parametrize(
        "field",
        [
            "momentum_signal",
            "mean_reversion_signal",
            "breakout_signal",
            "funding_signal",
            "open_interest_signal",
            "order_flow_imbalance_signal",
            "liquidity_sweep_signal",
        ],
    )
    def test_out_of_range_rejected(self, field: str) -> None:
        for val in [-1.001, 1.001, 2.0, -2.0, 100.0]:
            with pytest.raises(ValidationError, match=r"\[-1\.0, 1\.0\]"):
                _package(**{field: val})

    @pytest.mark.parametrize(
        "field",
        [
            "momentum_signal",
            "mean_reversion_signal",
            "breakout_signal",
            "funding_signal",
            "open_interest_signal",
            "order_flow_imbalance_signal",
            "liquidity_sweep_signal",
        ],
    )
    def test_none_accepted(self, field: str) -> None:
        pkg = _package(**{field: None})
        assert getattr(pkg, field) is None


# ---------------------------------------------------------------------------
# Validación de scores agregados [0.0, 1.0]
# ---------------------------------------------------------------------------


_AGGREGATE_SCORE_FIELDS = [
    "signal_strength_score",
    "signal_conflict_score",
    "signal_confidence",
]


class TestAggregateScoreValidation:
    @pytest.mark.parametrize("field", _AGGREGATE_SCORE_FIELDS)
    def test_boundary_values_accepted(self, field: str) -> None:
        for val in [0.0, 0.5, 1.0]:
            pkg = _package(**{field: val})
            assert getattr(pkg, field) == val

    @pytest.mark.parametrize("field", _AGGREGATE_SCORE_FIELDS)
    def test_below_zero_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _package(**{field: -0.001})

    @pytest.mark.parametrize("field", _AGGREGATE_SCORE_FIELDS)
    def test_above_one_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _package(**{field: 1.001})

    @pytest.mark.parametrize("field", _AGGREGATE_SCORE_FIELDS)
    def test_none_accepted(self, field: str) -> None:
        pkg = _package(**{field: None})
        assert getattr(pkg, field) is None


# ---------------------------------------------------------------------------
# Inmutabilidad
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_cannot_modify_signal(self) -> None:
        pkg = _package(momentum_signal=0.5)
        with pytest.raises(ValidationError):
            pkg.momentum_signal = 0.9  # type: ignore[misc]

    def test_cannot_modify_symbol(self) -> None:
        pkg = _package()
        with pytest.raises(ValidationError):
            pkg.symbol = "ETHUSDT"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Serialización to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_keys_match_quant_signals_orm(self) -> None:
        pkg = _package()
        bot_run_id = str(uuid.uuid4())
        kwargs = pkg.to_db_kwargs(bot_run_id)

        expected_keys = {
            "id",
            "bot_run_id",
            "market_snapshot_id",
            "symbol",
            "timestamp",
            "momentum_score",
            "mean_reversion_score",
            "breakout_score",
            "liquidity_sweep_score",
            "order_flow_score",
            "funding_signal",
            "open_interest_signal",
            "composite_score",
            "raw_signals",
        }
        assert set(kwargs.keys()) == expected_keys

    def test_signal_id_maps_to_id(self) -> None:
        fixed_id = str(uuid.uuid4())
        pkg = _package(signal_id=fixed_id)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["id"] == fixed_id

    def test_bot_run_id_forwarded(self) -> None:
        bot_run_id = str(uuid.uuid4())
        pkg = _package()
        kwargs = pkg.to_db_kwargs(bot_run_id)
        assert kwargs["bot_run_id"] == bot_run_id

    def test_snapshot_id_maps_to_market_snapshot_id(self) -> None:
        snap_id = str(uuid.uuid4())
        pkg = _package(snapshot_id=snap_id)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["market_snapshot_id"] == snap_id

    def test_signal_strength_score_maps_to_composite_score(self) -> None:
        pkg = _package(signal_strength_score=0.75)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["composite_score"] == 0.75

    def test_order_flow_imbalance_maps_to_order_flow_score(self) -> None:
        pkg = _package(order_flow_imbalance_signal=0.4)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["order_flow_score"] == 0.4

    def test_funding_signal_key_matches_orm_column_name(self) -> None:
        # La columna ORM se llama funding_signal (no funding_score)
        pkg = _package(funding_signal=-0.2)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["funding_signal"] == -0.2

    def test_open_interest_signal_key_matches_orm_column_name(self) -> None:
        # La columna ORM se llama open_interest_signal (no open_interest_score)
        pkg = _package(open_interest_signal=0.3)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["open_interest_signal"] == 0.3

    def test_raw_signals_contains_metadata(self) -> None:
        pkg = _package(
            signal_conflict_score=0.3,
            signal_confidence=0.9,
            timeframes_used=["1h", "4h"],
        )
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        raw = kwargs["raw_signals"]
        assert raw["signal_conflict_score"] == 0.3
        assert raw["signal_confidence"] == 0.9
        assert raw["timeframes_used"] == ["1h", "4h"]
        assert raw["version"] == SIGNAL_VERSION

    def test_raw_feature_refs_included_when_present(self) -> None:
        refs = {"rsi_14": 62.5, "ema_diff": 0.003}
        pkg = _package(raw_feature_refs=refs)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["raw_signals"]["raw_feature_refs"] == refs

    def test_raw_feature_refs_absent_when_none(self) -> None:
        pkg = _package()
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert "raw_feature_refs" not in kwargs["raw_signals"]

    def test_timestamp_utc_maps_to_timestamp(self) -> None:
        ts = datetime(2024, 6, 1, 10, 30, tzinfo=UTC)
        pkg = _package(timestamp_utc=ts)
        kwargs = pkg.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["timestamp"] == ts
