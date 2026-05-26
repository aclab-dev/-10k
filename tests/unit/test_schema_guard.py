"""Tests unitarios — SchemaGuard: contrato de bloqueo y auditoría de logs.

Cubre el comportamiento de validate_gpt_response más allá de la validación
de schema (ya cubierta en test_decision_schema.py): garantías de logging,
manejo de inputs no-dict, y el contrato de que ejecución bloqueada siempre
deja traza en structlog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from backend.decision_engine.schema_guard import SchemaGuardResult, validate_gpt_response
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)

_LOG_PATH = "backend.decision_engine.schema_guard._log"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _quant_signals() -> dict[str, object]:
    return QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    ).model_dump()


def _aggregator() -> dict[str, object]:
    return DecisionAggregatorSection(
        quant_score=0.8,
        gpt_context_score=0.85,
        risk_quality_score=0.75,
        final_trade_quality_score=0.80,
    ).model_dump()


def _news() -> dict[str, object]:
    return NewsContextSection(
        used=False, impact=NewsImpact.NEUTRAL, summary="No relevant news."
    ).model_dump()


def _position_plan() -> dict[str, object]:
    return PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
    ).model_dump()


def _valid_long(**overrides: object) -> dict[str, object]:
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


# ---------------------------------------------------------------------------
# Contrato de resultado
# ---------------------------------------------------------------------------


class TestSchemaGuardResult:
    def test_ok_result_is_truthy(self) -> None:
        r = SchemaGuardResult(ok=True, decision=None, errors=[])
        assert bool(r) is True

    def test_failed_result_is_falsy(self) -> None:
        r = SchemaGuardResult(ok=False, decision=None, errors=["field: error"])
        assert bool(r) is False

    def test_slots_prevent_extra_attributes(self) -> None:
        r = SchemaGuardResult(ok=True, decision=None, errors=[])
        try:
            r.unexpected = "value"  # type: ignore[attr-defined]
            raise AssertionError("should have raised AttributeError")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# Logging en caso de bloqueo (DoD: registra el motivo)
# ---------------------------------------------------------------------------


class TestSchemaGuardBlockedLogging:
    def test_blocked_event_logged_on_schema_failure(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response({"decision": "LONG"})
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "schema_guard.blocked"

    def test_blocked_log_contains_errors(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response({"decision": "LONG"})
        kwargs = mock_log.warning.call_args[1]
        assert kwargs["error_count"] >= 1
        assert isinstance(kwargs["errors"], list)
        assert all(isinstance(e, str) for e in kwargs["errors"])

    def test_blocked_log_contains_raw_keys(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response({"decision": "LONG", "symbol": "BTCUSDT"})
        kwargs = mock_log.warning.call_args[1]
        assert set(kwargs["raw_keys"]) == {"decision", "symbol"}

    def test_blocked_log_raw_keys_none_on_non_dict(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response("not-a-dict")  # type: ignore[arg-type]
        kwargs = mock_log.warning.call_args[1]
        assert kwargs["raw_keys"] is None
        assert kwargs["raw_type"] == "str"

    def test_blocked_log_reason_is_schema_validation_failed(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response({})
        kwargs = mock_log.warning.call_args[1]
        assert kwargs["reason"] == "schema_validation_failed"

    def test_blocked_log_calls_warning_not_error(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response({"decision": "INVALID"})
        mock_log.warning.assert_called_once()
        mock_log.error.assert_not_called()

    def test_invalid_symbol_blocked_and_logged(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            result = validate_gpt_response(_valid_long(symbol="DOGEUSDT"))
        assert not result.ok
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "schema_guard.blocked"
        assert any(
            "DOGEUSDT" in e or "símbolo" in e.lower() or "symbol" in e.lower()
            for e in result.errors
        )

    def test_margin_over_cap_blocked_and_logged(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            result = validate_gpt_response(_valid_long(margin_usdt=15.0))
        assert not result.ok
        mock_log.warning.assert_called_once()
        assert any("margin_usdt" in e for e in result.errors)

    def test_leverage_over_env_cap_blocked_and_logged(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            result = validate_gpt_response(_valid_long(environment="LIVE", leverage=8))
        assert not result.ok
        mock_log.warning.assert_called_once()

    def test_no_blocked_log_on_success(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response(_valid_long())
        mock_log.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Logging en caso de éxito
# ---------------------------------------------------------------------------


class TestSchemaGuardAcceptedLogging:
    def test_accepted_event_logged_on_valid_response(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response(_valid_long())
        mock_log.info.assert_called_once()
        assert mock_log.info.call_args[0][0] == "schema_guard.accepted"

    def test_accepted_log_contains_decision_fields(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response(_valid_long())
        kwargs = mock_log.info.call_args[1]
        assert kwargs["symbol"] == "BTCUSDT"
        assert kwargs["decision"] == DecisionType.LONG
        assert kwargs["environment"] == "PAPER"
        assert "decision_id" in kwargs

    def test_accepted_log_calls_info_not_warning(self) -> None:
        with patch(_LOG_PATH) as mock_log:
            validate_gpt_response(_valid_long())
        mock_log.info.assert_called_once()
        mock_log.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Inputs no-dict (excepción inesperada → bloqueo + log error)
# ---------------------------------------------------------------------------


class TestSchemaGuardUnexpectedInput:
    def test_none_input_returns_not_ok(self) -> None:
        result = validate_gpt_response(None)  # type: ignore[arg-type]
        assert not result.ok
        assert len(result.errors) > 0

    def test_string_input_returns_not_ok(self) -> None:
        result = validate_gpt_response("not a dict")  # type: ignore[arg-type]
        assert not result.ok

    def test_list_input_returns_not_ok(self) -> None:
        result = validate_gpt_response([{"decision": "LONG"}])  # type: ignore[arg-type]
        assert not result.ok

    def test_unexpected_error_logged_on_internal_exception(self) -> None:
        mock_log = MagicMock()
        with (
            patch(_LOG_PATH, mock_log),
            patch(
                "backend.decision_engine.schema_guard.ModelDecision.model_validate",
                side_effect=RuntimeError("internal failure"),
            ),
        ):
            result = validate_gpt_response(_valid_long())

        assert not result.ok
        mock_log.error.assert_called_once()
        assert mock_log.error.call_args[0][0] == "schema_guard.unexpected_error"
        assert mock_log.error.call_args[1]["reason"] == "unexpected_exception"

    def test_never_raises(self) -> None:
        """El guard nunca debe propagar excepciones, sin importar el input."""
        for bad_input in [None, "string", 42, [], object()]:
            result = validate_gpt_response(bad_input)  # type: ignore[arg-type]
            assert not result.ok


# ---------------------------------------------------------------------------
# Errores tienen formato path:mensaje
# ---------------------------------------------------------------------------


class TestSchemaGuardErrorFormat:
    def test_errors_contain_field_path(self) -> None:
        result = validate_gpt_response(_valid_long(symbol="INVALID"))
        assert not result.ok
        assert any(":" in e for e in result.errors)

    def test_multiple_errors_on_incomplete_payload(self) -> None:
        result = validate_gpt_response({"decision": "LONG", "symbol": "BTCUSDT"})
        assert not result.ok
        assert len(result.errors) >= 1

    def test_rr_below_minimum_blocked(self) -> None:
        result = validate_gpt_response(_valid_long(net_risk_reward=1.0))
        assert not result.ok
        assert any("net_risk_reward" in e or "1.5" in e for e in result.errors)
