"""Repositorios para el agregado Audit: SystemEvent, ErrorRecord, KillSwitchEvent, TokenUsage."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from backend.storage.models import ErrorRecord, KillSwitchEvent, SystemEvent, TokenUsage
from backend.storage.repositories.base import BaseRepository


class SystemEventRepository(BaseRepository[SystemEvent]):
    model = SystemEvent

    def list_by_severity(
        self, bot_run_id: str, severity: str, *, limit: int = 100
    ) -> list[SystemEvent]:
        stmt = (
            select(SystemEvent)
            .where(
                SystemEvent.bot_run_id == bot_run_id,
                SystemEvent.severity == severity,
            )
            .order_by(SystemEvent.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_by_event_type(
        self, bot_run_id: str, event_type: str, *, limit: int = 100
    ) -> list[SystemEvent]:
        stmt = (
            select(SystemEvent)
            .where(
                SystemEvent.bot_run_id == bot_run_id,
                SystemEvent.event_type == event_type,
            )
            .order_by(SystemEvent.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class ErrorRecordRepository(BaseRepository[ErrorRecord]):
    model = ErrorRecord

    def list_unrecovered(self, bot_run_id: str, *, limit: int = 100) -> list[ErrorRecord]:
        stmt = (
            select(ErrorRecord)
            .where(
                ErrorRecord.bot_run_id == bot_run_id,
                ErrorRecord.recovered.is_(False),
            )
            .order_by(ErrorRecord.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_by_module(
        self, bot_run_id: str, module: str, *, limit: int = 100
    ) -> list[ErrorRecord]:
        stmt = (
            select(ErrorRecord)
            .where(
                ErrorRecord.bot_run_id == bot_run_id,
                ErrorRecord.module == module,
            )
            .order_by(ErrorRecord.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class KillSwitchEventRepository(BaseRepository[KillSwitchEvent]):
    model = KillSwitchEvent

    def list_pending_review(self, bot_run_id: str) -> list[KillSwitchEvent]:
        stmt = (
            select(KillSwitchEvent)
            .where(
                KillSwitchEvent.bot_run_id == bot_run_id,
                KillSwitchEvent.requires_manual_review.is_(True),
            )
            .order_by(KillSwitchEvent.timestamp.desc())
        )
        return list(self._session.scalars(stmt))


class TokenUsageRepository(BaseRepository[TokenUsage]):
    model = TokenUsage

    def total_tokens_for_run(self, bot_run_id: str) -> int:
        """Suma total de tokens consumidos en el run."""
        stmt = select(func.sum(TokenUsage.total_tokens)).where(TokenUsage.bot_run_id == bot_run_id)
        result = self._session.scalar(stmt)
        return int(result) if result is not None else 0

    def list_by_model(self, bot_run_id: str, model: str, *, limit: int = 100) -> list[TokenUsage]:
        stmt = (
            select(TokenUsage)
            .where(TokenUsage.bot_run_id == bot_run_id, TokenUsage.model == model)
            .order_by(TokenUsage.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def tokens_in_window(self, bot_run_id: str, since: datetime) -> int:
        """Sum of total_tokens recorded for bot_run_id from `since` to now."""
        stmt = select(func.sum(TokenUsage.total_tokens)).where(
            TokenUsage.bot_run_id == bot_run_id,
            TokenUsage.timestamp >= since,
        )
        result = self._session.scalar(stmt)
        return int(result) if result is not None else 0

    def list_filtered(
        self,
        bot_run_id: str,
        *,
        model: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TokenUsage], int]:
        """Listado paginado de consumo de tokens, con filtro opcional por modelo.

        Devuelve (página ordenada DESC por timestamp, total que matchea el filtro).
        """
        filters = [TokenUsage.bot_run_id == bot_run_id]
        if model is not None:
            filters.append(TokenUsage.model == model)

        total = self._session.scalar(select(func.count()).select_from(TokenUsage).where(*filters))
        stmt = (
            select(TokenUsage)
            .where(*filters)
            .order_by(TokenUsage.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self._session.scalars(stmt))
        return items, int(total) if total is not None else 0
