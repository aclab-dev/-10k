"""Schemas del OrphanOrderScanner — hallazgos de reconciliación exchange vs. estado local."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class OrphanReason(StrEnum):
    """Motivo por el que un símbolo se marca como huérfano."""

    # Hay órdenes activas en el exchange para el símbolo pero ninguna posición
    # abierta que las explique.
    UNEXPLAINED_ORDER = "UNEXPLAINED_ORDER"
    # Hay una posición abierta en el exchange pero PositionManager no tiene un
    # PositionConfig activo vigilándola (SL/TP no monitoreado).
    UNPROTECTED_POSITION = "UNPROTECTED_POSITION"


class OrphanFinding(BaseModel):
    """Un hallazgo de orfandad para un símbolo puntual."""

    symbol: str
    reason: OrphanReason
    detail: str

    model_config = {"frozen": True}


__all__ = ["OrphanReason", "OrphanFinding"]
