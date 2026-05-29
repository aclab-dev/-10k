"""Tests unitarios para el contrato DecisionAggregationResult (sección 4.10.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from backend.decision_engine.aggregator_schemas import (
    AGGREGATION_VERSION,
    ContributingSources,
    DecisionAggregationResult,
)
from backend.decision_engine.schemas import DecisionType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _sources(**overrides: object) -> ContributingSources:
    defaults: dict[str, object] = {
        "quant_score": 0.7,
        "gpt_context_score": 0.8,
        "regime_factor": 0.6,
        "volatility_factor": 0.5,
    }
    defaults.update(overrides)
    return ContributingSources(**defaults)


def _result(**overrides: object) -> DecisionAggregationResult:
    """Construye un DecisionAggregationResult válido con valores mínimos."""
    defaults: dict[str, object] = {
        "decision_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "contributing_sources": _sources(),
        "aggregated_score": 0.72,
        "final_action": DecisionType.LONG,
    }
    defaults.update(overrides)
    return DecisionAggregationResult(**defaults)


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestDecisionAggregationResultConstruction:
    def test_minimal_fields_accepted(self) -> None:
        result = _result()
        assert result.symbol == "BTCUSDT"
        assert result.version == AGGREGATION_VERSION

    def test_aggregation_id_auto_generated(self) -> None:
        result = _result()
        assert uuid.UUID(result.aggregation_id)

    def test_two_instances_have_different_aggregation_ids(self) -> None:
        a = _result()
        b = _result()
        assert a.aggregation_id != b.aggregation_id

    def test_explicit_aggregation_id_preserved(self) -> None:
        fixed_id = str(uuid.uuid4())
        result = _result(aggregation_id=fixed_id)
        assert result.aggregation_id == fixed_id

    def test_contradictions_empty_by_default(self) -> None:
        result = _result()
        assert result.contradictions_detected == []

    def test_contradictions_preserved(self) -> None:
        result = _result(contradictions_detected=["quant vs GPT direction conflict"])
        assert len(result.contradictions_detected) == 1

    def test_all_symbols_accepted(self) -> None:
        for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"):
            r = _result(symbol=sym)
            assert r.symbol == sym

    def test_all_decision_types_accepted(self) -> None:
        for dt in DecisionType:
            r = _result(final_action=dt)
            assert r.final_action == dt

    def test_contributing_sources_stored(self) -> None:
        sources = _sources(quant_score=0.9, regime_factor=0.4)
        result = _result(contributing_sources=sources)
        assert result.contributing_sources.quant_score == 0.9
        assert result.contributing_sources.regime_factor == 0.4

    def test_is_frozen(self) -> None:
        result = _result()
        with pytest.raises(ValidationError):
            result.aggregated_score = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validadores — aggregation_id y decision_id
# ---------------------------------------------------------------------------


class TestUUIDValidation:
    def test_invalid_aggregation_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            _result(aggregation_id="not-a-uuid")

    def test_invalid_decision_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            _result(decision_id="bad-id")

    def test_valid_uuid_strings_accepted(self) -> None:
        valid = str(uuid.uuid4())
        result = _result(decision_id=valid)
        assert result.decision_id == valid


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
        from datetime import timedelta

        tz_plus3 = timezone(timedelta(hours=3))
        with pytest.raises(ValidationError, match="UTC"):
            _result(timestamp_utc=datetime(2024, 1, 1, 12, 0, 0, tzinfo=tz_plus3))

    def test_utc_datetime_accepted(self) -> None:
        ts = datetime(2024, 6, 15, 10, 30, 0, tzinfo=UTC)
        result = _result(timestamp_utc=ts)
        assert result.timestamp_utc == ts


# ---------------------------------------------------------------------------
# Validadores — aggregated_score
# ---------------------------------------------------------------------------


class TestAggregatedScoreValidation:
    def test_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(aggregated_score=-0.01)

    def test_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(aggregated_score=1.01)

    def test_boundary_values_accepted(self) -> None:
        assert _result(aggregated_score=0.0).aggregated_score == 0.0
        assert _result(aggregated_score=1.0).aggregated_score == 1.0


# ---------------------------------------------------------------------------
# Validadores — ContributingSources
# ---------------------------------------------------------------------------


class TestContributingSourcesValidation:
    @pytest.mark.parametrize(
        "field",
        ["quant_score", "gpt_context_score", "regime_factor", "volatility_factor"],
    )
    def test_source_below_zero_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _sources(**{field: -0.01})

    @pytest.mark.parametrize(
        "field",
        ["quant_score", "gpt_context_score", "regime_factor", "volatility_factor"],
    )
    def test_source_above_one_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _sources(**{field: 1.01})

    def test_all_boundary_zeros_accepted(self) -> None:
        s = _sources(quant_score=0.0, gpt_context_score=0.0, regime_factor=0.0, volatility_factor=0.0)
        assert s.quant_score == 0.0

    def test_all_boundary_ones_accepted(self) -> None:
        s = _sources(quant_score=1.0, gpt_context_score=1.0, regime_factor=1.0, volatility_factor=1.0)
        assert s.volatility_factor == 1.0

    def test_contributing_sources_is_frozen(self) -> None:
        s = _sources()
        with pytest.raises(ValidationError):
            s.quant_score = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Serialización — to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_required_keys_present(self) -> None:
        result = _result()
        bot_run_id = str(uuid.uuid4())
        kwargs = result.to_db_kwargs(bot_run_id)

        assert kwargs["id"] == result.aggregation_id
        assert kwargs["bot_run_id"] == bot_run_id
        assert kwargs["decision_id"] == result.decision_id
        assert kwargs["symbol"] == result.symbol
        assert kwargs["timestamp"] == result.timestamp_utc
        assert kwargs["aggregated_score"] == result.aggregated_score
        assert kwargs["final_action"] == result.final_action.value

    def test_contributing_sources_mapped_correctly(self) -> None:
        sources = _sources(quant_score=0.9, gpt_context_score=0.75, regime_factor=0.6, volatility_factor=0.4)
        result = _result(contributing_sources=sources)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))

        assert kwargs["quant_score"] == 0.9
        assert kwargs["gpt_confidence"] == 0.75
        assert kwargs["regime_factor"] == 0.6
        assert kwargs["volatility_factor"] == 0.4

    def test_reasons_contains_contradictions(self) -> None:
        conflicts = ["quant bearish vs GPT bullish"]
        result = _result(contradictions_detected=conflicts)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))

        assert kwargs["reasons"]["contradictions_detected"] == conflicts

    def test_reasons_contains_version(self) -> None:
        result = _result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["reasons"]["version"] == AGGREGATION_VERSION

    def test_final_action_serialized_as_string(self) -> None:
        result = _result(final_action=DecisionType.SHORT)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["final_action"] == "SHORT"

    def test_no_operar_serialized_correctly(self) -> None:
        result = _result(final_action=DecisionType.NO_OPERAR)
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["final_action"] == "NO_OPERAR"
