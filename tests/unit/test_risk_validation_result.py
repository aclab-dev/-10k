"""Tests unitarios para el contrato RiskValidationResult (sección 4.10.9)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.risk_engine.schemas import (
    RISK_VALIDATION_VERSION,
    AdjustedParameters,
    RiskDecision,
    RiskValidationResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _adj(**overrides: object) -> AdjustedParameters:
    defaults: dict[str, object] = {"margin_usdt": Decimal("5.0"), "leverage": 5}
    defaults.update(overrides)
    return AdjustedParameters(**defaults)


def _result(**overrides: object) -> RiskValidationResult:
    """Construye un RiskValidationResult válido con APPROVE por defecto."""
    defaults: dict[str, object] = {
        "aggregation_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "decision": RiskDecision.APPROVE,
        "original_margin_usdt": Decimal("5.0"),
        "original_leverage": 5,
        "adjusted_parameters": _adj(),
        "reasons": {"margin_cap": "dentro del límite de 10 USDT"},
    }
    defaults.update(overrides)
    return RiskValidationResult(**defaults)


def _block(**overrides: object) -> RiskValidationResult:
    """Construye un RiskValidationResult válido con BLOCK."""
    defaults: dict[str, object] = {
        "aggregation_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "decision": RiskDecision.BLOCK,
        "original_margin_usdt": Decimal("5.0"),
        "original_leverage": 5,
        "adjusted_parameters": None,
        "reasons": {"daily_loss": "drawdown diario superado"},
    }
    defaults.update(overrides)
    return RiskValidationResult(**defaults)


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestRiskValidationResultConstruction:
    def test_approve_minimal_fields_accepted(self) -> None:
        result = _result()
        assert result.symbol == "BTCUSDT"
        assert result.decision == RiskDecision.APPROVE
        assert result.version == RISK_VALIDATION_VERSION

    def test_block_minimal_fields_accepted(self) -> None:
        result = _block()
        assert result.decision == RiskDecision.BLOCK
        assert result.adjusted_parameters is None

    def test_adjust_down_accepted(self) -> None:
        result = _result(
            decision=RiskDecision.ADJUST_DOWN,
            adjusted_parameters=_adj(margin_usdt=Decimal("3.0"), leverage=3),
        )
        assert result.decision == RiskDecision.ADJUST_DOWN
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("3.0")

    def test_validation_id_auto_generated(self) -> None:
        result = _result()
        assert uuid.UUID(result.validation_id)

    def test_two_instances_have_different_validation_ids(self) -> None:
        a = _result()
        b = _result()
        assert a.validation_id != b.validation_id

    def test_explicit_validation_id_preserved(self) -> None:
        fixed = str(uuid.uuid4())
        result = _result(validation_id=fixed)
        assert result.validation_id == fixed

    def test_all_symbols_accepted(self) -> None:
        for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"):
            r = _result(symbol=sym)
            assert r.symbol == sym

    def test_optional_loss_fields_default_none(self) -> None:
        result = _result()
        assert result.daily_loss_at_check_usdt is None
        assert result.total_loss_at_check_usdt is None

    def test_optional_loss_fields_accepted(self) -> None:
        result = _result(
            daily_loss_at_check_usdt=Decimal("2.5"),
            total_loss_at_check_usdt=Decimal("7.0"),
        )
        assert result.daily_loss_at_check_usdt == Decimal("2.5")
        assert result.total_loss_at_check_usdt == Decimal("7.0")

    def test_negative_loss_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(daily_loss_at_check_usdt=Decimal("-0.01"))
        with pytest.raises(ValidationError):
            _result(total_loss_at_check_usdt=Decimal("-0.01"))

    def test_zero_loss_fields_accepted(self) -> None:
        result = _result(
            daily_loss_at_check_usdt=Decimal("0"),
            total_loss_at_check_usdt=Decimal("0"),
        )
        assert result.daily_loss_at_check_usdt == Decimal("0")
        assert result.total_loss_at_check_usdt == Decimal("0")

    def test_reasons_stored(self) -> None:
        reasons = {"sl_check": "SL presente", "margin_cap": "dentro del límite"}
        result = _result(reasons=reasons)
        assert result.reasons == reasons

    def test_empty_reasons_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(reasons={})

    def test_is_frozen(self) -> None:
        result = _result()
        with pytest.raises(ValidationError):
            result.decision = RiskDecision.BLOCK  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validadores — validation_id y aggregation_id
# ---------------------------------------------------------------------------


class TestUUIDValidation:
    def test_invalid_validation_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            _result(validation_id="not-a-uuid")

    def test_invalid_aggregation_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            _result(aggregation_id="bad-id")

    def test_valid_uuid_strings_accepted(self) -> None:
        valid = str(uuid.uuid4())
        result = _result(aggregation_id=valid)
        assert result.aggregation_id == valid


# ---------------------------------------------------------------------------
# Validadores — symbol
# ---------------------------------------------------------------------------


class TestSymbolValidation:
    def test_invalid_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            _result(symbol="DOGEUSDT")

    def test_empty_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(symbol="")


# ---------------------------------------------------------------------------
# Validadores — timestamp_utc
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTC"):
            _result(timestamp_utc=datetime(2024, 1, 1, 12, 0, 0))

    def test_non_utc_timezone_rejected(self) -> None:
        tz_plus3 = timezone(timedelta(hours=3))
        with pytest.raises(ValidationError, match="UTC"):
            _result(timestamp_utc=datetime(2024, 1, 1, 12, 0, 0, tzinfo=tz_plus3))

    def test_utc_datetime_accepted(self) -> None:
        ts = datetime(2024, 6, 15, 10, 30, 0, tzinfo=UTC)
        result = _result(timestamp_utc=ts)
        assert result.timestamp_utc == ts


# ---------------------------------------------------------------------------
# Validadores — original_margin_usdt y original_leverage
# ---------------------------------------------------------------------------


class TestOriginalParametersValidation:
    def test_margin_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(original_margin_usdt=Decimal("0"))

    def test_margin_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(original_margin_usdt=Decimal("10.01"))

    def test_margin_exactly_max_accepted(self) -> None:
        result = _result(
            original_margin_usdt=Decimal("10.0"),
            adjusted_parameters=_adj(margin_usdt=Decimal("10.0")),
        )
        assert result.original_margin_usdt == Decimal("10.0")

    def test_leverage_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(original_leverage=0)

    def test_leverage_above_ten_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(original_leverage=11)

    def test_leverage_boundary_values_accepted(self) -> None:
        assert _result(original_leverage=1).original_leverage == 1
        assert _result(original_leverage=10).original_leverage == 10


# ---------------------------------------------------------------------------
# Validadores — AdjustedParameters
# ---------------------------------------------------------------------------


class TestAdjustedParametersValidation:
    def test_margin_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _adj(margin_usdt=Decimal("0"))

    def test_margin_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _adj(margin_usdt=Decimal("10.01"))

    def test_leverage_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _adj(leverage=0)

    def test_leverage_above_ten_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _adj(leverage=11)

    def test_is_frozen(self) -> None:
        adj = _adj()
        with pytest.raises(ValidationError):
            adj.leverage = 3  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validador de modelo — coherencia decision / adjusted_parameters
# ---------------------------------------------------------------------------


class TestAdjustedParametersCoherence:
    def test_block_with_adjusted_parameters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="BLOCK"):
            _block(adjusted_parameters=_adj())

    def test_approve_without_adjusted_parameters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="APPROVE"):
            _result(decision=RiskDecision.APPROVE, adjusted_parameters=None)

    def test_adjust_down_without_adjusted_parameters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ADJUST_DOWN"):
            _result(decision=RiskDecision.ADJUST_DOWN, adjusted_parameters=None)

    def test_block_without_adjusted_parameters_accepted(self) -> None:
        result = _block()
        assert result.adjusted_parameters is None

    def test_approve_with_adjusted_parameters_accepted(self) -> None:
        result = _result(decision=RiskDecision.APPROVE, adjusted_parameters=_adj())
        assert result.adjusted_parameters is not None

    def test_adjust_down_with_adjusted_parameters_accepted(self) -> None:
        result = _result(
            decision=RiskDecision.ADJUST_DOWN,
            adjusted_parameters=_adj(margin_usdt=Decimal("2.0"), leverage=2),
        )
        assert result.adjusted_parameters.margin_usdt == Decimal("2.0")

    def test_adjust_down_with_identical_parameters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ADJUST_DOWN"):
            _result(
                decision=RiskDecision.ADJUST_DOWN,
                original_margin_usdt=Decimal("5.0"),
                original_leverage=5,
                adjusted_parameters=_adj(margin_usdt=Decimal("5.0"), leverage=5),
            )

    def test_adjust_down_only_margin_reduced_accepted(self) -> None:
        result = _result(
            decision=RiskDecision.ADJUST_DOWN,
            original_margin_usdt=Decimal("8.0"),
            original_leverage=5,
            adjusted_parameters=_adj(margin_usdt=Decimal("5.0"), leverage=5),
        )
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt < result.original_margin_usdt

    def test_adjust_down_only_leverage_reduced_accepted(self) -> None:
        result = _result(
            decision=RiskDecision.ADJUST_DOWN,
            original_margin_usdt=Decimal("5.0"),
            original_leverage=8,
            adjusted_parameters=_adj(margin_usdt=Decimal("5.0"), leverage=5),
        )
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.leverage < result.original_leverage

    def test_adjust_down_with_higher_adjusted_values_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ADJUST_DOWN"):
            _result(
                decision=RiskDecision.ADJUST_DOWN,
                original_margin_usdt=Decimal("3.0"),
                original_leverage=3,
                adjusted_parameters=_adj(margin_usdt=Decimal("5.0"), leverage=5),
            )


# ---------------------------------------------------------------------------
# Serialización — to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_required_keys_present_on_approve(self) -> None:
        result = _result()
        bot_run_id = str(uuid.uuid4())
        kwargs = result.to_db_kwargs(bot_run_id)

        assert kwargs["id"] == result.validation_id
        assert kwargs["bot_run_id"] == bot_run_id
        assert kwargs["decision_aggregation_id"] == result.aggregation_id
        assert kwargs["symbol"] == result.symbol
        assert kwargs["timestamp"] == result.timestamp_utc
        assert kwargs["result"] == "APPROVE"

    def test_original_parameters_mapped(self) -> None:
        result = _result(original_margin_usdt=Decimal("8.0"), original_leverage=7)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["original_margin"] == Decimal("8.0")
        assert kwargs["original_leverage"] == 7

    def test_adjusted_parameters_mapped_on_approve(self) -> None:
        adj = _adj(margin_usdt=Decimal("5.0"), leverage=5)
        result = _result(adjusted_parameters=adj)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["adjusted_margin"] == Decimal("5.0")
        assert kwargs["adjusted_leverage"] == 5

    def test_adjusted_parameters_none_on_block(self) -> None:
        result = _block()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["adjusted_margin"] is None
        assert kwargs["adjusted_leverage"] is None

    def test_result_serialized_as_string(self) -> None:
        approve_kwargs = _result(decision=RiskDecision.APPROVE).to_db_kwargs(str(uuid.uuid4()))
        assert approve_kwargs["result"] == "APPROVE"
        assert _block().to_db_kwargs(str(uuid.uuid4()))["result"] == "BLOCK"
        assert (
            _result(
                decision=RiskDecision.ADJUST_DOWN,
                adjusted_parameters=_adj(margin_usdt=Decimal("3.0"), leverage=3),
            ).to_db_kwargs(str(uuid.uuid4()))["result"]
            == "ADJUST_DOWN"
        )

    def test_loss_fields_mapped(self) -> None:
        result = _result(
            daily_loss_at_check_usdt=Decimal("1.5"),
            total_loss_at_check_usdt=Decimal("4.0"),
        )
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["daily_loss_at_check"] == Decimal("1.5")
        assert kwargs["total_loss_at_check"] == Decimal("4.0")

    def test_loss_fields_none_when_not_set(self) -> None:
        kwargs = _result().to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["daily_loss_at_check"] is None
        assert kwargs["total_loss_at_check"] is None

    def test_reasons_mapped(self) -> None:
        reasons = {"sl_missing": "sin stop-loss definido"}
        result = _block(reasons=reasons)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["reasons"] == reasons

    def test_invalid_bot_run_id_raises(self) -> None:
        result = _result()
        with pytest.raises(ValueError, match="UUID"):
            result.to_db_kwargs("not-a-uuid")
