"""Schemas del Execution Engine (F10/CR)."""

from __future__ import annotations

from dataclasses import dataclass

from backend.exchange_adapters.schemas import OrderResult


@dataclass(frozen=True)
class ExecutionResult:
    """Resultado de ejecutar un ApprovedTradePlan (ModelDecision + RiskValidationResult).

    Envuelve el `OrderResult` del adapter con las referencias de persistencia
    (Trade/Order en DB) y si la posición quedó registrada en PositionManager.
    `trade_id` es `None` cuando la orden no llegó a FILLED (p.ej. LIMIT pendiente).
    """

    order_result: OrderResult
    trade_id: str | None
    order_db_id: str
    position_registered: bool


__all__ = ["ExecutionResult"]
