"""Endpoints de validaciones del Risk Engine (incluye la vista de bloqueos)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.api.dependencies import TimeRange, get_current_bot_run_id
from backend.api.pagination import Page, PageParams
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.risk_engine.schemas import RiskDecision
from backend.storage.database import get_db
from backend.storage.repositories import RiskValidationRepository

router = APIRouter(prefix="/risk", tags=["risk"])

_ALLOWED_RESULTS = frozenset(d.value for d in RiskDecision)


class RiskValidationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bot_run_id: str
    symbol: str
    timestamp: datetime
    result: str
    original_margin: Decimal | None
    original_leverage: int | None
    adjusted_margin: Decimal | None
    adjusted_leverage: int | None
    reasons: dict[str, object] | None
    daily_loss_at_check: Decimal | None
    total_loss_at_check: Decimal | None


@router.get("/validations")
def list_risk_validations(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends()],
    time_range: Annotated[TimeRange, Depends()],
    symbol: Annotated[str | None, Query(description="Filtra por símbolo, ej. BTCUSDT")] = None,
    result: Annotated[
        str | None,
        Query(
            description="Filtra por resultado: APPROVE | ADJUST_DOWN | BLOCK | NO_OPERAR. "
            "Usar BLOCK para la vista de bloqueos."
        ),
    ] = None,
) -> Page[RiskValidationOut]:
    """Listado paginado de validaciones del Risk Engine, con filtros por fecha, símbolo y result."""
    if symbol is not None and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"symbol inválido: '{symbol}'")
    if result is not None and result not in _ALLOWED_RESULTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"result inválido: '{result}'")

    items, total = RiskValidationRepository(db).list_filtered(
        bot_run_id,
        symbol=symbol,
        result=result,
        from_ts=time_range.from_ts,
        to_ts=time_range.to_ts,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[RiskValidationOut](
        items=[RiskValidationOut.model_validate(i) for i in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
