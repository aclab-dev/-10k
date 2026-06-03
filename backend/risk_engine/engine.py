"""Risk Engine — sección 4.10.9 del PDF maestro.

Recibe un DecisionAggregationResult + el ModelDecision original y emite un
RiskValidationResult con decisión APPROVE / ADJUST_DOWN / BLOCK.

Orden de precedencia:
1. Checks BLOCK (drawdown, SL ausente, TP/exit plan ausente).
   Cualquier falla → BLOCK inmediato.
2. Checks ADJUST_DOWN (margin cap, leverage cap).
   Al menos una falla → ADJUST_DOWN con parámetros reducidos.
3. Sin fallas → APPROVE con los parámetros originales.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.core.config import AppConfig
from backend.decision_engine.aggregator_schemas import DecisionAggregationResult
from backend.decision_engine.schemas import ModelDecision
from backend.risk_engine.adjustments import compute_adjusted_leverage, compute_adjusted_margin
from backend.risk_engine.checks import (
    CheckOutcome,
    CheckResult,
    check_daily_drawdown,
    check_leverage_cap,
    check_liquidation_safety,
    check_margin_cap,
    check_sl_required,
    check_total_drawdown,
    check_tp_or_exit_plan,
)
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult


def validate(
    aggregation: DecisionAggregationResult,
    decision: ModelDecision,
    daily_loss_usdt: Decimal,
    total_loss_usdt: Decimal,
    config: AppConfig,
) -> RiskValidationResult:
    """Valida el DecisionAggregationResult y emite un RiskValidationResult.

    Precondiciones:
    - decision.execute debe ser True (el engine solo procesa decisiones ejecutables).
    - decision.margin_usdt debe ser > 0.

    Args:
        aggregation: resultado del Decision Aggregator (F8).
        decision: ModelDecision original del GPT Context Evaluator.
        daily_loss_usdt: pérdida realizada en el día corriente (≥0, en USDT).
        total_loss_usdt: pérdida total acumulada del bot (≥0, en USDT).
        config: configuración de la aplicación.

    Returns:
        RiskValidationResult con decisión APPROVE / ADJUST_DOWN / BLOCK.

    Raises:
        ValueError: si execute=False o margin_usdt ≤ 0.
    """
    if not decision.execute:
        raise ValueError(
            "validate() requiere decision.execute=True; "
            "el Risk Engine solo procesa decisiones ejecutables."
        )
    if decision.margin_usdt <= 0:
        raise ValueError(
            f"decision.margin_usdt debe ser > 0 para decisiones ejecutables, "
            f"recibido: {decision.margin_usdt}."
        )

    max_margin = Decimal(str(config.risk.max_margin_per_trade_usdt))
    initial_balance = Decimal(str(config.challenge.initial_balance_usdt))
    environment = config.execution.environment
    original_margin = Decimal(str(decision.margin_usdt))
    original_leverage = decision.leverage

    # -----------------------------------------------------------------------
    # Fase 1: checks BLOCK
    # -----------------------------------------------------------------------
    block_checks: list[CheckResult] = [
        check_sl_required(decision),
        check_tp_or_exit_plan(decision),
        check_daily_drawdown(daily_loss_usdt, initial_balance, config.risk.max_daily_loss_percent),
        check_total_drawdown(total_loss_usdt, initial_balance, config.risk.max_total_loss_percent),
        check_liquidation_safety(decision, config.liquidation_safety),
    ]

    block_reasons: dict[str, str] = {
        c.rule: c.reason for c in block_checks if c.outcome == CheckOutcome.BLOCK
    }

    if block_reasons:
        # Incluir también los checks que pasaron para trazabilidad completa.
        all_reasons = {c.rule: c.reason for c in block_checks}
        return RiskValidationResult(
            aggregation_id=aggregation.aggregation_id,
            symbol=decision.symbol,
            timestamp_utc=datetime.now(UTC),
            decision=RiskDecision.BLOCK,
            original_margin_usdt=original_margin,
            original_leverage=original_leverage,
            adjusted_parameters=None,
            daily_loss_at_check_usdt=daily_loss_usdt,
            total_loss_at_check_usdt=total_loss_usdt,
            reasons=all_reasons,
        )

    # -----------------------------------------------------------------------
    # Fase 2: checks ADJUST_DOWN
    # -----------------------------------------------------------------------
    adjust_checks: list[CheckResult] = [
        check_margin_cap(original_margin, max_margin),
        check_leverage_cap(original_leverage, config, environment),
    ]

    reasons: dict[str, str] = {c.rule: c.reason for c in block_checks}
    reasons.update({c.rule: c.reason for c in adjust_checks})

    needs_adjustment = any(c.outcome == CheckOutcome.ADJUST_DOWN for c in adjust_checks)

    if needs_adjustment:
        adjusted_margin = compute_adjusted_margin(original_margin, max_margin)
        adjusted_leverage = compute_adjusted_leverage(original_leverage, config, environment)
        return RiskValidationResult(
            aggregation_id=aggregation.aggregation_id,
            symbol=decision.symbol,
            timestamp_utc=datetime.now(UTC),
            decision=RiskDecision.ADJUST_DOWN,
            original_margin_usdt=original_margin,
            original_leverage=original_leverage,
            adjusted_parameters=AdjustedParameters(
                margin_usdt=adjusted_margin,
                leverage=adjusted_leverage,
            ),
            daily_loss_at_check_usdt=daily_loss_usdt,
            total_loss_at_check_usdt=total_loss_usdt,
            reasons=reasons,
        )

    # -----------------------------------------------------------------------
    # Fase 3: APPROVE
    # -----------------------------------------------------------------------
    return RiskValidationResult(
        aggregation_id=aggregation.aggregation_id,
        symbol=decision.symbol,
        timestamp_utc=datetime.now(UTC),
        decision=RiskDecision.APPROVE,
        original_margin_usdt=original_margin,
        original_leverage=original_leverage,
        adjusted_parameters=AdjustedParameters(
            margin_usdt=original_margin,
            leverage=original_leverage,
        ),
        daily_loss_at_check_usdt=daily_loss_usdt,
        total_loss_at_check_usdt=total_loss_usdt,
        reasons=reasons,
    )
