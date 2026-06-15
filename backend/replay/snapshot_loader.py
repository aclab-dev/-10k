"""Loader de snapshots históricos desde market_snapshots (F11)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.replay.schemas import SnapshotWindow
from backend.storage.models import MarketSnapshot
from backend.storage.repositories import MarketSnapshotRepository


class SnapshotLoader:
    """Carga snapshots históricos desde market_snapshots por símbolo y rango de fechas."""

    def __init__(self, session: Session) -> None:
        self._repo = MarketSnapshotRepository(session)

    def load(
        self,
        window: SnapshotWindow,
        bot_run_id: str | None = None,
    ) -> list[MarketSnapshot]:
        """Devuelve snapshots de market_snapshots dentro del rango [period_start, period_end).

        Ordena por timestamp ASC para reproducción cronológica.
        Si se provee bot_run_id, filtra solo ese run; si no, consulta todos los runs.
        """
        return self._repo.list_by_symbol_and_range(
            symbol=window.symbol,
            period_start=window.period_start,
            period_end=window.period_end,
            bot_run_id=bot_run_id,
        )
