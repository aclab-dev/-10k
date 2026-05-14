from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.models import BotRun
from backend.storage.repositories.base import BaseRepository


class BotRunRepository(BaseRepository[BotRun]):
    _model = BotRun

    def get_by_id(self, session: Session, run_id: str) -> BotRun | None:
        return session.get(BotRun, run_id)

    def get_active_runs(self, session: Session) -> list[BotRun]:
        return session.scalars(
            select(BotRun).where(BotRun.status == "RUNNING")
        ).all()

    def mark_finished(self, session: Session, run_id: str, status: str) -> BotRun | None:
        run = session.get(BotRun, run_id)
        if run is None:
            return None
        run.status = status
        session.flush()
        return run
