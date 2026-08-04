"""Endpoints de consumo de tokens GPT y estado del budget."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_bot_run_id
from backend.api.pagination import Page, PageParams
from backend.storage.database import get_db
from backend.storage.repositories import TokenUsageRepository
from backend.token_budget.manager import TokenBudgetManager
from backend.token_budget.schemas import BudgetStatus

router = APIRouter(prefix="/tokens", tags=["tokens"])


class TokenUsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bot_run_id: str
    timestamp: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: Decimal | None


class TokenBudgetStatusOut(BaseModel):
    status: BudgetStatus
    ok: bool
    tokens_used_hour: int
    limit_hour: int
    tokens_used_day: int
    limit_day: int


@router.get("/usage")
def list_token_usage(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends()],
    model: Annotated[str | None, Query(description="Filtra por nombre de modelo GPT")] = None,
) -> Page[TokenUsageOut]:
    """Listado paginado de consumo de tokens por request GPT."""
    items, total = TokenUsageRepository(db).list_filtered(
        bot_run_id, model=model, limit=page.limit, offset=page.offset
    )
    return Page[TokenUsageOut](
        items=[TokenUsageOut.model_validate(i) for i in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/budget")
def get_token_budget(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
) -> TokenBudgetStatusOut:
    """Estado actual del budget de tokens (consumo hora/día vs límites configurados)."""
    result = TokenBudgetManager(session=db, bot_run_id=bot_run_id).get_current_status()
    return TokenBudgetStatusOut(
        status=result.status,
        ok=result.ok,
        tokens_used_hour=result.tokens_used_hour,
        limit_hour=result.limit_hour,
        tokens_used_day=result.tokens_used_day,
        limit_day=result.limit_day,
    )
