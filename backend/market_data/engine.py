"""Market Data Engine — valida snapshots y los persiste en market_snapshots."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.market_data.schemas import MarketSnapshot as MarketSnapshotSchema
from backend.market_data.validators import validate_snapshot
from backend.storage.models import MarketSnapshot as MarketSnapshotORM
from backend.storage.repositories.snapshots import MarketSnapshotRepository


class MarketDataEngine:
    """Punto de entrada para procesar un snapshot de mercado.

    Aplica el gate de validación (frescura + coherencia) y persiste el
    snapshot en market_snapshots si pasa. Lanza SnapshotRejectedError si no.
    """

    def __init__(self, session: Session, bot_run_id: str) -> None:
        self._repo = MarketSnapshotRepository(session)
        self._bot_run_id = bot_run_id

    def process_snapshot(self, snapshot: MarketSnapshotSchema) -> MarketSnapshotORM:
        """Valida y persiste un snapshot. Devuelve el registro ORM creado."""
        validated = validate_snapshot(snapshot)
        return self._repo.save_snapshot(validated, self._bot_run_id)
