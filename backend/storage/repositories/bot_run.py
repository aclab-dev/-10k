from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.storage.models import BotRun


class BotRunRepository:
    def create(self, session: Session, **kwargs: Any) -> BotRun:
        run = BotRun(**kwargs)
        session.add(run)
        session.flush()
        return run

    def get_by_id(self, session: Session, run_id: str) -> BotRun | None:
        return session.get(BotRun, run_id)

    def get_active_runs(self, session: Session) -> list[BotRun]:
        return session.query(BotRun).filter(BotRun.status == "RUNNING").all()

    def mark_finished(self, session: Session, run_id: str, status: str) -> BotRun | None:
        run = session.get(BotRun, run_id)
        if run is None:
            return None
        run.status = status
        session.flush()
        return run
