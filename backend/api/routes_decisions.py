"""Endpoints de historial de decisiones del Decision Aggregator."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_bot_run_id, raise_404_if_not_uuid
from backend.api.pagination import Page, PageParams
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.storage.database import get_db
from backend.storage.repositories import DecisionRepository

router = APIRouter(prefix="/decisions", tags=["decisions"])

_ALLOWED_ACTIONS = frozenset({"OPEN", "CLOSE", "NO_OPERAR"})


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bot_run_id: str
    symbol: str
    timestamp: datetime
    action: str
    direction: str | None
    confidence: float | None
    margin_usdt: Decimal | None
    leverage: int | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    reasoning: str | None


@router.get("")
def list_decisions(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends()],
    symbol: Annotated[str | None, Query(description="Filtra por símbolo, ej. BTCUSDT")] = None,
    action: Annotated[
        str | None, Query(description="Filtra por acción: OPEN | CLOSE | NO_OPERAR")
    ] = None,
) -> Page[DecisionOut]:
    """Listado paginado de decisiones, con filtros opcionales por símbolo y acción."""
    if symbol is not None and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"symbol inválido: '{symbol}'")
    if action is not None and action not in _ALLOWED_ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"action inválida: '{action}'")

    items, total = DecisionRepository(db).list_filtered(
        bot_run_id, symbol=symbol, action=action, limit=page.limit, offset=page.offset
    )
    return Page[DecisionOut](
        items=[DecisionOut.model_validate(i) for i in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{decision_id}")
def get_decision(decision_id: str, db: Annotated[Session, Depends(get_db)]) -> DecisionOut:
    """Detalle de una decisión puntual."""
    raise_404_if_not_uuid(decision_id, param_name="decision_id")
    decision = DecisionRepository(db).get_by_id(decision_id)
    if decision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"decision_id '{decision_id}' no encontrada")
    return DecisionOut.model_validate(decision)
