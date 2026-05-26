"""Tests unitarios — GPTDecisionResponse y schema_guard (sección 3.8)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.core.config import Environment
from backend.decision_engine.schema_guard import validate_gpt_response
from backend.decision_engine.schemas import (
    DECISION_SCHEMA_VERSION,
    DecisionAction,
    GPTDecisionResponse,
    TradeDirection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHALLENGE_MODE = "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK"


def _open_long(**overrides: object) -> dict[str, object]:
    """Dict válido para decision=OPEN LONG en PAPER."""
    base: dict[str, object] = {
        "challenge_mode": _CHALLENGE_MODE,
        "environment": "PAPER",
        "decision": "OPEN",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "leverage": 5,
        "margin_usdt": "5.0",
        "stop_loss": "90000.0",
        "take_profit": "100000.0",
        "confidence": 0.85,
        "reasoning": "Momentum + breakout confirm bullish setup.",
    }
    base.update(overrides)
    return base


def _build(**overrides: object) -> GPTDecisionResponse:
    return GPTDecisionResponse.model_validate(_open_long(**overrides))


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestGPTDecisionResponseConstruction:
    def test_valid_open_long_accepted(self) -> None:
        d = _build()
        assert d.decision == DecisionAction.OPEN
        assert d.direction == TradeDirection.LONG
        assert d.symbol == "BTCUSDT"
        assert d.schema_version == DECISION_SCHEMA_VERSION

    def test_decision_id_auto_generated(self) -> None:
        d = _build()
        assert uuid.UUID(d.decision_id)  # es UUID válido

    def test_two_instances_have_different_decision_ids(self) -> None:
        a = _build()
        b = _build()
        assert a.decision_id != b.decision_id

    def test_explicit_decision_id_preserved(self) -> None:
        fixed = str(uuid.uuid4())
        d = _build(decision_id=fixed)
        assert d.decision_id == fixed

    def test_model_is_frozen(self) -> None:
        d = _build()
        with pytest.raises(ValidationError):
            d.symbol = "ETHUSDT"  # frozen model raises ValidationError at runtime

    def test_valid_open_short_accepted(self) -> None:
        d = _build(
            direction="SHORT",
            stop_loss="100000.0",
            take_profit="90000.0",
        )
        assert d.direction == TradeDirection.SHORT

    def test_valid_close_accepted(self) -> None:
        d = GPTDecisionResponse(
            challenge_mode=_CHALLENGE_MODE,
            environment=Environment.PAPER,
            decision=DecisionAction.CLOSE,
            symbol="ETHUSDT",
            reasoning="Position hit take-profit. Closing.",
        )
        assert d.decision == DecisionAction.CLOSE
        assert d.direction is None

    def test_valid_no_operar_accepted(self) -> None:
        d = GPTDecisionResponse(
            challenge_mode=_CHALLENGE_MODE,
            environment=Environment.TESTNET,
            decision=DecisionAction.NO_OPERAR,
            symbol="SOLUSDT",
            reasoning="No clear signal. Staying out of market.",
        )
        assert d.decision == DecisionAction.NO_OPERAR


# ---------------------------------------------------------------------------
# Validación de symbol
# ---------------------------------------------------------------------------


class TestSymbolValidation:
    @pytest.mark.parametrize("sym", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    def test_all_allowed_symbols_accepted(self, sym: str) -> None:
        d = _build(symbol=sym)
        assert d.symbol == sym

    def test_unknown_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            _build(symbol="DOGEUSDT")

    def test_lowercase_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(symbol="btcusdt")


# ---------------------------------------------------------------------------
# Validación de decision == OPEN (campos requeridos)
# ---------------------------------------------------------------------------


class TestOpenRequiresFullSpec:
    @pytest.mark.parametrize(
        "missing_field",
        ["direction", "leverage", "margin_usdt", "stop_loss", "take_profit"],
    )
    def test_open_missing_required_field_rejected(self, missing_field: str) -> None:
        data = _open_long()
        data[missing_field] = None
        with pytest.raises(ValidationError, match="OPEN requiere"):
            GPTDecisionResponse.model_validate(data)


# ---------------------------------------------------------------------------
# Hard cap de margen
# ---------------------------------------------------------------------------


class TestMarginHardCap:
    def test_margin_at_cap_accepted(self) -> None:
        d = _build(margin_usdt="10.0")
        assert d.margin_usdt == Decimal("10.0")

    def test_margin_above_cap_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hard cap"):
            _build(margin_usdt="10.01")

    def test_margin_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(margin_usdt="0")

    def test_margin_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(margin_usdt="-1")


# ---------------------------------------------------------------------------
# Caps de leverage por entorno
# ---------------------------------------------------------------------------


class TestLeverageCaps:
    def test_leverage_10_in_paper_accepted(self) -> None:
        d = _build(environment="PAPER", leverage=10)
        assert d.leverage == 10

    def test_leverage_11_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(leverage=11)

    def test_leverage_5_in_testnet_accepted(self) -> None:
        d = _build(environment="TESTNET", leverage=5)
        assert d.leverage == 5

    def test_leverage_6_in_testnet_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cap"):
            _build(environment="TESTNET", leverage=6)

    def test_leverage_5_in_live_accepted(self) -> None:
        d = _build(environment="LIVE", leverage=5)
        assert d.leverage == 5

    def test_leverage_6_in_live_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cap"):
            _build(environment="LIVE", leverage=6)

    def test_leverage_0_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(leverage=0)


# ---------------------------------------------------------------------------
# SL / TP coherencia direccional
# ---------------------------------------------------------------------------


class TestSlTpCoherence:
    def test_long_sl_below_tp_accepted(self) -> None:
        d = _build(stop_loss="85000", take_profit="95000")
        assert d.stop_loss is not None and d.take_profit is not None
        assert d.stop_loss < d.take_profit

    def test_long_sl_equal_tp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="LONG"):
            _build(stop_loss="90000", take_profit="90000")

    def test_long_sl_above_tp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="LONG"):
            _build(stop_loss="95000", take_profit="85000")

    def test_short_sl_above_tp_accepted(self) -> None:
        d = _build(
            direction="SHORT",
            stop_loss="95000",
            take_profit="85000",
        )
        assert d.stop_loss is not None and d.take_profit is not None
        assert d.stop_loss > d.take_profit

    def test_short_sl_below_tp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SHORT"):
            _build(direction="SHORT", stop_loss="85000", take_profit="95000")


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidenceRange:
    @pytest.mark.parametrize("val", [0.0, 0.5, 1.0])
    def test_valid_confidence_accepted(self, val: float) -> None:
        d = _build(confidence=val)
        assert d.confidence == val

    def test_confidence_above_1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(confidence=1.01)

    def test_confidence_below_0_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(confidence=-0.01)

    def test_confidence_none_accepted(self) -> None:
        d = _build(confidence=None)
        assert d.confidence is None


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


class TestReasoning:
    def test_short_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(reasoning="ok")

    def test_empty_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(reasoning="")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_invalid_environment_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(environment="STAGING")

    @pytest.mark.parametrize("env", ["PAPER", "TESTNET", "LIVE"])
    def test_all_valid_environments_accepted(self, env: str) -> None:
        d = _build(environment=env)
        assert d.environment == Environment(env)


# ---------------------------------------------------------------------------
# to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_returns_required_keys(self) -> None:
        d = _build()
        bot_run_id = str(uuid.uuid4())
        kwargs = d.to_db_kwargs(bot_run_id=bot_run_id)
        for key in ("id", "bot_run_id", "symbol", "action", "direction", "raw_decision"):
            assert key in kwargs

    def test_bot_run_id_propagated(self) -> None:
        d = _build()
        run_id = str(uuid.uuid4())
        kwargs = d.to_db_kwargs(bot_run_id=run_id)
        assert kwargs["bot_run_id"] == run_id

    def test_model_response_id_optional(self) -> None:
        d = _build()
        kwargs = d.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert kwargs["model_response_id"] is None

    def test_raw_decision_is_dict(self) -> None:
        d = _build()
        kwargs = d.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert isinstance(kwargs["raw_decision"], dict)


# ---------------------------------------------------------------------------
# schema_guard
# ---------------------------------------------------------------------------


class TestSchemaGuard:
    def test_valid_dict_returns_ok(self) -> None:
        result = validate_gpt_response(_open_long())
        assert result.ok
        assert result.decision is not None
        assert result.errors == []

    def test_result_bool_true_on_ok(self) -> None:
        result = validate_gpt_response(_open_long())
        assert bool(result) is True

    def test_invalid_dict_returns_not_ok(self) -> None:
        result = validate_gpt_response({"decision": "OPEN"})
        assert not result.ok
        assert result.decision is None
        assert len(result.errors) > 0

    def test_result_bool_false_on_failure(self) -> None:
        result = validate_gpt_response({})
        assert bool(result) is False

    def test_errors_are_strings(self) -> None:
        result = validate_gpt_response({"decision": "OPEN", "symbol": "INVALID"})
        assert all(isinstance(e, str) for e in result.errors)

    def test_valid_close_returns_ok(self) -> None:
        result = validate_gpt_response(
            {
                "challenge_mode": _CHALLENGE_MODE,
                "environment": "PAPER",
                "decision": "CLOSE",
                "symbol": "BTCUSDT",
                "reasoning": "Position closed at target level.",
            }
        )
        assert result.ok

    def test_margin_over_cap_returns_not_ok(self) -> None:
        data = _open_long(margin_usdt="15.0")
        result = validate_gpt_response(data)
        assert not result.ok
        assert any("hard cap" in e for e in result.errors)
