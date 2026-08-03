"""Endpoints de estado del bot: bot run activo, bot state, PnL y drawdown."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_bot_run_id
from backend.api.pagination import Page, PageParams
from backend.storage.database import get_db
from backend.storage.repositories import (
    AccountStateRepository,
    BotRunRepository,
    BotStateRepository,
)

router = APIRouter(prefix="/status", tags=["status"])


class AccountStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    balance_usdt: Decimal
    equity_usdt: Decimal
    margin_used_usdt: Decimal
    unrealized_pnl: Decimal
    realized_pnl_session: Decimal
    drawdown_percent: float
    exposure_percent: float


class BotStatusOut(BaseModel):
    bot_run_id: str
    environment: str
    app_version: str
    run_status: str
    started_at: datetime
    ended_at: datetime | None
    state: str | None
    previous_state: str | None
    state_reason: str | None
    state_updated_at: datetime | None
    account: AccountStateOut | None


@router.get("")
def get_status(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
) -> BotStatusOut:
    """Estado actual del bot: run activo, último BotState y snapshot de cuenta (PnL/drawdown)."""
    bot_run = BotRunRepository(db).get_by_id(bot_run_id)
    if bot_run is None:
        # No debería pasar: get_current_bot_run_id ya validó que bot_run_id existe.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"bot_run_id '{bot_run_id}' no encontrado")

    bot_state = BotStateRepository(db).get_latest(bot_run_id)
    account_state = AccountStateRepository(db).get_latest(bot_run_id)

    return BotStatusOut(
        bot_run_id=bot_run.id,
        environment=bot_run.environment,
        app_version=bot_run.app_version,
        run_status=bot_run.status,
        started_at=bot_run.started_at,
        ended_at=bot_run.ended_at,
        state=bot_state.state if bot_state else None,
        previous_state=bot_state.previous_state if bot_state else None,
        state_reason=bot_state.reason if bot_state else None,
        state_updated_at=bot_state.created_at if bot_state else None,
        account=AccountStateOut.model_validate(account_state) if account_state else None,
    )


@router.get("/history")
def get_status_history(
    bot_run_id: Annotated[str, Depends(get_current_bot_run_id)],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends()],
    since: Annotated[datetime | None, Query(description="Filtra timestamp >= since")] = None,
    until: Annotated[datetime | None, Query(description="Filtra timestamp <= until")] = None,
) -> Page[AccountStateOut]:
    """Serie temporal de balance/equity/PnL/drawdown, paginada — para el gráfico de equity."""
    items, total = AccountStateRepository(db).list_history(
        bot_run_id, since=since, until=until, limit=page.limit, offset=page.offset
    )
    return Page[AccountStateOut](
        items=[AccountStateOut.model_validate(i) for i in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
