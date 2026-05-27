"""Tests unitarios — JSON Schema Guard (F7).

Cubre validate_gpt_response (dict) y validate_gpt_json_string (raw string).
Cualquier respuesta fuera de schema debe retornar ok=False con al menos un
error descriptivo; nunca debe propagar una excepción al caller.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from backend.decision_engine.schema_guard import (
    SchemaGuardResult,
    validate_gpt_json_string,
    validate_gpt_response,
)
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    ModelDecision,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _quant_signals() -> dict:
    return QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    ).model_dump()


def _aggregator() -> dict:
    return DecisionAggregatorSection(
        quant_score=0.8,
        gpt_context_score=0.85,
        risk_quality_score=0.75,
        final_trade_quality_score=0.80,
    ).model_dump()


def _news() -> dict:
    return NewsContextSection(
        used=False,
        impact=NewsImpact.NEUTRAL,
        summary="No relevant news.",
    ).model_dump()


def _position_plan() -> dict:
    return PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
    ).model_dump()


def _valid_long(**overrides: object) -> dict:
    """Dict válido para LONG ejecutable en PAPER."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now(),
        "decision": "LONG",
        "symbol": "BTCUSDT",
        "entry_type": "MARKET",
        "entry_price": 95000.0,
        "stop_loss": 90000.0,
        "take_profit": 105000.0,
        "invalidation_price": 89000.0,
        "leverage": 5,
        "margin_usdt": 5.0,
        "estimated_notional_usdt": 25.0,
        "estimated_entry_fee_usdt": 0.025,
        "estimated_exit_fee_usdt": 0.025,
        "estimated_slippage_usdt": 0.05,
        "estimated_funding_usdt": -0.01,
        "net_risk_reward": 2.0,
        "estimated_max_loss_usdt": 5.0,
        "liquidation_distance_percent_estimated": 18.0,
        "confidence": 0.82,
        "market_regime": "TRENDING",
        "setup_name": "momentum_breakout",
        "timeframes_used": ["15m", "1h", "4h"],
        "quant_signals": _quant_signals(),
        "decision_aggregator": _aggregator(),
        "news_context": _news(),
        "position_management_plan": _position_plan(),
        "decision_rationale_summary": "Strong momentum with breakout confirmed on 1h.",
        "execute": True,
    }
    base.update(overrides)
    return base


def _valid_no_operar(**overrides: object) -> dict:
    base = _valid_long(
        decision="NO_OPERAR",
        entry_type="NO_ENTRY",
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        invalidation_price=0.0,
        margin_usdt=0.0,
        net_risk_reward=0.0,
        execute=False,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# SchemaGuardResult — contrato del tipo
# ---------------------------------------------------------------------------


class TestSchemaGuardResultContract:
    def test_ok_true_bools_to_true(self) -> None:
        r = SchemaGuardResult(ok=True, decision=None, errors=[])
        assert bool(r) is True

    def test_ok_false_bools_to_false(self) -> None:
        r = SchemaGuardResult(ok=False, decision=None, errors=["err"])
        assert bool(r) is False

    def test_result_is_immutable(self) -> None:
        r = SchemaGuardResult(ok=True, decision=None, errors=[])
        with pytest.raises((AttributeError, TypeError)):
            r.ok = False  # type: ignore[misc]

    def test_ok_result_has_empty_errors(self) -> None:
        result = validate_gpt_response(_valid_long())
        assert result.ok
        assert result.errors == []

    def test_failed_result_has_no_decision(self) -> None:
        result = validate_gpt_response({})
        assert not result.ok
        assert result.decision is None

    def test_failed_result_errors_are_strings(self) -> None:
        result = validate_gpt_response({"decision": "LONG"})
        assert all(isinstance(e, str) for e in result.errors)
        assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# validate_gpt_response — happy path
# ---------------------------------------------------------------------------


class TestValidateGptResponseHappyPath:
    def test_valid_long_returns_ok(self) -> None:
        result = validate_gpt_response(_valid_long())
        assert result.ok
        assert isinstance(result.decision, ModelDecision)

    def test_valid_short_returns_ok(self) -> None:
        result = validate_gpt_response(
            _valid_long(
                decision="SHORT",
                stop_loss=100000.0,
                take_profit=85000.0,
            )
        )
        assert result.ok
        assert result.decision is not None
        assert result.decision.execute is True

    def test_valid_no_operar_returns_ok(self) -> None:
        result = validate_gpt_response(_valid_no_operar())
        assert result.ok
        assert result.decision is not None
        assert result.decision.execute is False

    def test_accepted_decision_type_matches(self) -> None:
        result = validate_gpt_response(_valid_long())
        assert result.decision is not None
        assert result.decision.symbol == "BTCUSDT"
        assert result.decision.environment.value == "PAPER"

    @pytest.mark.parametrize(
        ("env", "leverage"),
        [
            ("PAPER", 10),  # cap PAPER = 10x
            ("TESTNET", 5),  # cap TESTNET = 5x
            ("LIVE", 5),  # cap LIVE = 5x
        ],
    )
    def test_all_environments_accepted_at_cap(self, env: str, leverage: int) -> None:
        """Leverage exactamente en el cap de cada entorno debe ser aceptado."""
        result = validate_gpt_response(_valid_long(environment=env, leverage=leverage))
        assert result.ok

    @pytest.mark.parametrize("sym", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    def test_all_allowed_symbols_accepted(self, sym: str) -> None:
        assert validate_gpt_response(_valid_long(symbol=sym)).ok


# ---------------------------------------------------------------------------
# validate_gpt_response — bloqueos de schema
# ---------------------------------------------------------------------------


class TestValidateGptResponseBlocked:
    def test_empty_dict_blocked(self) -> None:
        result = validate_gpt_response({})
        assert not result.ok
        assert result.errors

    def test_missing_required_fields_blocked(self) -> None:
        result = validate_gpt_response({"decision": "LONG"})
        assert not result.ok
        assert len(result.errors) > 1

    def test_invalid_symbol_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(symbol="DOGEUSDT"))
        assert not result.ok
        assert any("no permitido" in e for e in result.errors)

    def test_invalid_decision_type_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(decision="MAYBE"))
        assert not result.ok

    def test_invalid_environment_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(environment="STAGING"))
        assert not result.ok

    def test_margin_at_boundary_accepted(self) -> None:
        result = validate_gpt_response(_valid_long(margin_usdt=10.0))
        assert result.ok

    def test_margin_above_10_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(margin_usdt=10.01))
        assert not result.ok

    def test_leverage_above_10_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(leverage=11))
        assert not result.ok

    def test_leverage_above_testnet_cap_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(environment="TESTNET", leverage=6))
        assert not result.ok
        assert any("cap" in e for e in result.errors)

    def test_leverage_above_live_cap_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(environment="LIVE", leverage=6))
        assert not result.ok

    def test_confidence_at_zero_accepted(self) -> None:
        result = validate_gpt_response(_valid_long(confidence=0.0))
        assert result.ok

    def test_confidence_at_one_accepted(self) -> None:
        result = validate_gpt_response(_valid_long(confidence=1.0))
        assert result.ok

    def test_confidence_above_1_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(confidence=1.01))
        assert not result.ok

    def test_confidence_below_0_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(confidence=-0.01))
        assert not result.ok

    def test_invalid_challenge_mode_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(challenge_mode="WRONG_MODE"))
        assert not result.ok

    def test_invalid_decision_id_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(decision_id="not-a-uuid"))
        assert not result.ok


# ---------------------------------------------------------------------------
# validate_gpt_response — reglas de negocio
# ---------------------------------------------------------------------------


class TestValidateGptResponseBusinessRules:
    def test_no_operar_with_execute_true_blocked(self) -> None:
        result = validate_gpt_response(
            _valid_long(
                decision="NO_OPERAR",
                entry_type="NO_ENTRY",
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                execute=True,
            )
        )
        assert not result.ok
        assert any("execute" in e for e in result.errors)

    def test_execute_true_with_sl_zero_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(stop_loss=0.0))
        assert not result.ok
        assert any("stop_loss" in e for e in result.errors)

    def test_execute_true_with_tp_zero_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(take_profit=0.0))
        assert not result.ok
        assert any("take_profit" in e for e in result.errors)

    def test_rr_below_1_5_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(net_risk_reward=1.4))
        assert not result.ok
        assert any("net_risk_reward" in e for e in result.errors)

    def test_rr_exactly_1_5_accepted(self) -> None:
        result = validate_gpt_response(_valid_long(net_risk_reward=1.5))
        assert result.ok

    def test_long_sl_above_entry_blocked(self) -> None:
        result = validate_gpt_response(
            _valid_long(entry_price=95000.0, stop_loss=96000.0, take_profit=105000.0)
        )
        assert not result.ok
        assert any("LONG" in e for e in result.errors)

    def test_long_tp_below_entry_blocked(self) -> None:
        result = validate_gpt_response(
            _valid_long(entry_price=95000.0, stop_loss=90000.0, take_profit=94000.0)
        )
        assert not result.ok

    def test_short_sl_below_entry_blocked(self) -> None:
        result = validate_gpt_response(
            _valid_long(
                decision="SHORT",
                entry_price=95000.0,
                stop_loss=90000.0,
                take_profit=85000.0,
            )
        )
        assert not result.ok
        assert any("SHORT" in e for e in result.errors)

    def test_short_tp_above_entry_blocked(self) -> None:
        result = validate_gpt_response(
            _valid_long(
                decision="SHORT",
                entry_price=95000.0,
                stop_loss=100000.0,
                take_profit=96000.0,
            )
        )
        assert not result.ok

    def test_sl_tp_not_checked_when_not_execute(self) -> None:
        result = validate_gpt_response(_valid_no_operar())
        assert result.ok

    def test_rr_not_checked_when_not_execute(self) -> None:
        result = validate_gpt_response(_valid_no_operar(net_risk_reward=0.0))
        assert result.ok


# ---------------------------------------------------------------------------
# validate_gpt_response — inputs no-dict (nunca deben propagar excepción)
# ---------------------------------------------------------------------------


class TestValidateGptResponseNonDict:
    def test_list_input_returns_not_ok(self) -> None:
        result = validate_gpt_response([])  # type: ignore[arg-type]
        assert not result.ok

    def test_none_input_returns_not_ok(self) -> None:
        result = validate_gpt_response(None)  # type: ignore[arg-type]
        assert not result.ok

    def test_string_input_returns_not_ok(self) -> None:
        result = validate_gpt_response("LONG")  # type: ignore[arg-type]
        assert not result.ok

    def test_integer_input_returns_not_ok(self) -> None:
        result = validate_gpt_response(42)  # type: ignore[arg-type]
        assert not result.ok


# ---------------------------------------------------------------------------
# validate_gpt_json_string — happy path
# ---------------------------------------------------------------------------


class TestValidateGptJsonStringHappyPath:
    def test_valid_json_string_returns_ok(self) -> None:
        raw = json.dumps(_valid_long())
        result = validate_gpt_json_string(raw)
        assert result.ok
        assert isinstance(result.decision, ModelDecision)

    def test_valid_no_operar_json_string_returns_ok(self) -> None:
        raw = json.dumps(_valid_no_operar())
        result = validate_gpt_json_string(raw)
        assert result.ok

    def test_result_decision_matches_input(self) -> None:
        d = _valid_long(symbol="ETHUSDT")
        result = validate_gpt_json_string(json.dumps(d))
        assert result.ok
        assert result.decision is not None
        assert result.decision.symbol == "ETHUSDT"

    def test_json_with_extra_whitespace_accepted(self) -> None:
        raw = "  " + json.dumps(_valid_long()) + "  "
        result = validate_gpt_json_string(raw)
        assert result.ok


# ---------------------------------------------------------------------------
# validate_gpt_json_string — parse errors
# ---------------------------------------------------------------------------


class TestValidateGptJsonStringParseErrors:
    def test_invalid_json_syntax_blocked(self) -> None:
        result = validate_gpt_json_string("{decision: LONG}")
        assert not result.ok
        assert any("json_parse_error" in e for e in result.errors)

    def test_empty_string_blocked(self) -> None:
        result = validate_gpt_json_string("")
        assert not result.ok

    def test_truncated_json_blocked(self) -> None:
        raw = json.dumps(_valid_long())[:50]
        result = validate_gpt_json_string(raw)
        assert not result.ok

    def test_plain_text_blocked(self) -> None:
        result = validate_gpt_json_string("I cannot provide a trading decision.")
        assert not result.ok

    def test_errors_are_strings(self) -> None:
        result = validate_gpt_json_string("not json")
        assert all(isinstance(e, str) for e in result.errors)
        assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# validate_gpt_json_string — JSON válido pero schema inválido
# ---------------------------------------------------------------------------


class TestValidateGptJsonStringSchemaViolations:
    def test_json_array_blocked(self) -> None:
        result = validate_gpt_json_string("[]")
        assert not result.ok
        assert any("object" in e.lower() or "array" in e.lower() for e in result.errors)

    def test_json_null_blocked(self) -> None:
        result = validate_gpt_json_string("null")
        assert not result.ok

    def test_json_number_blocked(self) -> None:
        result = validate_gpt_json_string("42")
        assert not result.ok

    def test_json_string_blocked(self) -> None:
        result = validate_gpt_json_string('"LONG"')
        assert not result.ok

    def test_empty_object_blocked(self) -> None:
        result = validate_gpt_json_string("{}")
        assert not result.ok

    def test_schema_violation_in_json_string_blocked(self) -> None:
        d = _valid_long(margin_usdt=999.0)
        result = validate_gpt_json_string(json.dumps(d))
        assert not result.ok

    def test_business_rule_violation_in_json_string_blocked(self) -> None:
        d = _valid_long(net_risk_reward=0.5)
        result = validate_gpt_json_string(json.dumps(d))
        assert not result.ok
        assert any("net_risk_reward" in e for e in result.errors)

    def test_invalid_symbol_in_json_string_blocked(self) -> None:
        d = _valid_long(symbol="DOGECOIN")
        result = validate_gpt_json_string(json.dumps(d))
        assert not result.ok

    def test_no_operar_execute_true_in_json_string_blocked(self) -> None:
        d = _valid_long(
            decision="NO_OPERAR",
            entry_type="NO_ENTRY",
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            execute=True,
        )
        result = validate_gpt_json_string(json.dumps(d))
        assert not result.ok
