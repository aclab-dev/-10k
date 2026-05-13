"""Repositorios para el agregado Bot: BotRun, BotState, AccountState."""

from __future__ import annotations

from sqlalchemy import select

from backend.storage.models import AccountState, BotRun, BotState
from backend.storage.repositories.base import BaseRepository


class BotRunRepository(BaseRepository[BotRun]):
    model = BotRun

    def get_active(self) -> BotRun | None:
        """Devuelve el BotRun en estado RUNNING, si existe."""
        stmt = select(BotRun).where(BotRun.status == "RUNNING").limit(1)
        return self._session.scalars(stmt).first()

    def close(self, bot_run: BotRun, status: str = "STOPPED") -> BotRun:
        from datetime import UTC, datetime

        bot_run.status = status
        bot_run.ended_at = datetime.now(UTC)
        self._session.flush()
        return bot_run


class BotStateRepository(BaseRepository[BotState]):
    model = BotState

    def get_latest(self, bot_run_id: str) -> BotState | None:
        stmt = (
            select(BotState)
            .where(BotState.bot_run_id == bot_run_id)
            .order_by(BotState.created_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class AccountStateRepository(BaseRepository[AccountState]):
    model = AccountState

    def get_latest(self, bot_run_id: str) -> AccountState | None:
        stmt = (
            select(AccountState)
            .where(AccountState.bot_run_id == bot_run_id)
            .order_by(AccountState.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()
