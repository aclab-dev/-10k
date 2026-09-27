"""Tests unitarios — gate de fees pre-trade del Risk Engine (F17, regla 12)."""

from __future__ import annotations

from decimal import Decimal

from backend.core.config import AppConfig, get_config
from backend.core.fees import FeeEstimate, FeeRates, estimate_fees_for_decision
from backend.decision_engine.schemas import ModelDecision
from backend.risk_engine.checks import CheckOutcome, check_fee_gate
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import RiskDecision, RiskValidationResult
from tests.unit.test_risk_engine_validation import _aggregation, _long_decision

_D = Decimal
_PAPER_RATES = FeeRates(maker=_D("0.0002"), taker=_D("0.0005"))


def _fees(decision: ModelDecision, rates: FeeRates = _PAPER_RATES) -> FeeEstimate:
    return estimate_fees_for_decision(
        decision, _D(str(decision.margin_usdt)), decision.leverage, rates
    )


def _scalp_decision() -> ModelDecision:
    """SL a 0.1% y TP a 0.2%: RR bruto 2.0 (lo que declara GPT), pero el
    round-trip taker (~0.1% del notional) lo deja muy por debajo de 1.5."""
    return _long_decision(entry_price=95000.0, stop_loss=94905.0, take_profit=95190.0)


def _validate(
    decision: ModelDecision,
    fee_estimate: FeeEstimate | None,
    config: AppConfig | None = None,
    daily_loss_usdt: Decimal = _D("0"),
) -> RiskValidationResult:
    return validate(
        _aggregation(decision),
        decision,
        daily_loss_usdt,
        _D("0"),
        config or get_config(),
        funding_rate=0.0001,
        open_positions_count=0,
        fee_estimate=fee_estimate,
    )


# ---------------------------------------------------------------------------
# check_fee_gate
# ---------------------------------------------------------------------------


def test_gate_passes_and_reports_fees_when_net_rr_is_enough() -> None:
    est = _fees(_long_decision())
    result = check_fee_gate(est, min_net_risk_reward=1.5)
    assert result.outcome == CheckOutcome.PASS
    assert result.rule == "fee_gate"
    assert result.reason == est.as_audit_reason()


def test_gate_blocks_when_fees_push_net_rr_below_minimum() -> None:
    est = _fees(_scalp_decision())
    assert est.gross_risk_reward >= _D("1.5")
    result = check_fee_gate(est, min_net_risk_reward=1.5)
    assert result.outcome == CheckOutcome.BLOCK
    assert str(est.net_risk_reward) in result.reason


def test_gate_threshold_is_inclusive() -> None:
    est = _fees(_long_decision())
    at = float(est.net_risk_reward)
    assert check_fee_gate(est, min_net_risk_reward=at).outcome == CheckOutcome.PASS
    above = float(est.net_risk_reward + _D("0.0001"))
    assert check_fee_gate(est, min_net_risk_reward=above).outcome == CheckOutcome.BLOCK


def test_gate_blocks_fail_closed_without_estimate() -> None:
    result = check_fee_gate(None, min_net_risk_reward=1.5)
    assert result.outcome == CheckOutcome.BLOCK
    assert "obligatorio" in result.reason


def test_gate_uses_the_rates_it_is_given() -> None:
    """El umbral se cruza o no según la tasa real del adapter, no una constante."""
    decision = _long_decision(entry_price=95000.0, stop_loss=94050.0, take_profit=96900.0)
    cheap = _fees(decision, FeeRates(maker=_D("0"), taker=_D("0.0001")))
    pricey = _fees(decision, FeeRates(maker=_D("0.0002"), taker=_D("0.001")))
    assert check_fee_gate(cheap, 1.5).outcome == CheckOutcome.PASS
    assert check_fee_gate(pricey, 1.5).outcome == CheckOutcome.BLOCK


# ---------------------------------------------------------------------------
# Integración con validate()
# ---------------------------------------------------------------------------


def test_validate_approves_normal_trade_with_fee_in_reasons() -> None:
    decision = _long_decision()
    est = _fees(decision)
    result = _validate(decision, est)
    assert result.decision == RiskDecision.APPROVE
    assert result.reasons["fee_gate"] == est.as_audit_reason()


def test_validate_blocks_trade_whose_edge_is_eaten_by_fees() -> None:
    decision = _scalp_decision()
    result = _validate(decision, _fees(decision))
    assert result.decision == RiskDecision.BLOCK
    assert result.adjusted_parameters is None
    assert "RR neto de fees" in result.reasons["fee_gate"]


def test_validate_blocks_without_fee_estimate() -> None:
    decision = _long_decision()
    result = _validate(decision, None)
    assert result.decision == RiskDecision.BLOCK
    assert "fee_gate" in result.reasons


def test_fee_is_audited_when_another_rule_blocks() -> None:
    """El fee proyectado queda en `reasons` también si bloquea otra regla."""
    decision = _long_decision()
    est = _fees(decision)
    cfg = get_config()
    # max_daily_loss_percent está en porcentaje (10.0 = 10%).
    daily_limit = (
        _D(str(cfg.challenge.initial_balance_usdt)) * _D(str(cfg.risk.max_daily_loss_percent)) / 100
    )
    result = _validate(decision, est, cfg, daily_loss_usdt=daily_limit)
    assert result.decision == RiskDecision.BLOCK
    assert result.reasons["fee_gate"] == est.as_audit_reason()


def test_validate_uses_configured_minimum() -> None:
    decision = _long_decision()
    est = _fees(decision)
    cfg = get_config()
    strict = cfg.model_copy(
        update={
            "risk": cfg.risk.model_copy(
                update={"min_net_risk_reward": float(est.net_risk_reward) + 0.1}
            )
        }
    )
    assert _validate(decision, est, strict).decision == RiskDecision.BLOCK
