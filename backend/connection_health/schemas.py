"""Schemas del ConnectionHealthMonitor — anomalías de conectividad/reloj (F16 [117])."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ConnectionAnomalyReason(StrEnum):
    """Motivo por el que un símbolo se marca con anomalía de conectividad."""

    # El símbolo no aparece entre los snapshots exitosos de este ciclo: el fetch
    # falló (ya agotó los reintentos + circuit breaker de F16 [113]) o el
    # snapshot fue rechazado por el Market Data Guard (stale/incoherente/clock
    # skew fuera del bound duro de MarketSnapshot). No distinguimos la causa
    # exacta acá — MarketDataCycleService.tick_all() ya la logueó en detalle —
    # solo que no hay dato utilizable para ese símbolo en este ciclo.
    SYMBOL_DATA_UNAVAILABLE = "SYMBOL_DATA_UNAVAILABLE"
    # clock_skew_ms del snapshot supera el umbral configurado (mas estricto que
    # el bound duro de MarketSnapshot, que solo rechaza la construccion).
    CLOCK_SKEW_EXCEEDED = "CLOCK_SKEW_EXCEEDED"
    # latency_ms del snapshot supera el umbral configurado.
    LATENCY_EXCEEDED = "LATENCY_EXCEEDED"


class ConnectionAnomalyFinding(BaseModel):
    """Un hallazgo de anomalía de conectividad para un símbolo puntual."""

    symbol: str
    reason: ConnectionAnomalyReason
    detail: str

    model_config = {"frozen": True}


__all__ = ["ConnectionAnomalyReason", "ConnectionAnomalyFinding"]
