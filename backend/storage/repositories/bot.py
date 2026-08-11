"""Repositorios para el agregado Bot: BotRun, BotState, AccountState."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from backend.storage.models import AccountState, BotRun, BotState
from backend.storage.repositories.base import BaseRepository


class BotRunRepository(BaseRepository[BotRun]):
    model = BotRun

    def get_active(self) -> BotRun | None:
        """Devuelve el BotRun en estado RUNNING, si existe."""
        stmt = select(BotRun).where(BotRun.status == "RUNNING").limit(1)
        return self._session.scalars(stmt).first()

    def get_most_recent(self) -> BotRun | None:
        """Devuelve el BotRun mas nuevo (cualquier status), o None si no hay ninguno.

        Usado al arrancar un worker nuevo para decidir si hay que arrastrar el
        ultimo estado conocido (ver Orchestrator._resolve_carried_over_state).
        """
        stmt = select(BotRun).order_by(BotRun.started_at.desc()).limit(1)
        return self._session.scalars(stmt).first()

    def close(self, bot_run: BotRun, status: str = "STOPPED") -> BotRun:
        bot_run.status = status
        bot_run.ended_at = datetime.now(UTC)
        return self.save(bot_run)


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

    def list_history(
        self,
        bot_run_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AccountState], int]:
        """Serie temporal de AccountState (balance/equity/PnL/drawdown) para el dashboard.

        Devuelve (página ordenada ASC por timestamp, total de registros que matchean el filtro).
        """
        filters = [AccountState.bot_run_id == bot_run_id]
        if since is not None:
            filters.append(AccountState.timestamp >= since)
        if until is not None:
            filters.append(AccountState.timestamp <= until)

        total = self._session.scalar(select(func.count()).select_from(AccountState).where(*filters))
        stmt = (
            select(AccountState)
            .where(*filters)
            .order_by(AccountState.timestamp.asc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self._session.scalars(stmt))
        return items, int(total) if total is not None else 0
