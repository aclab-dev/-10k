"""Schemas del PositionManager — configuración y resultados de monitoreo de posiciones."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class PositionTriggerReason(StrEnum):
    NONE = "NONE"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    TRAILING_SL_HIT = "TRAILING_SL_HIT"


class PositionConfig(BaseModel):
    """Configuración de salida para una posición abierta.

    Al menos uno de stop_loss, take_profit o trailing_delta debe estar presente
    para que el tick tenga efecto (si todos son None, el manager siempre retorna NONE).
    """

    symbol: str
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"))
    # Distancia fija en unidades de precio que el trailing stop mantiene respecto al high-water.
    trailing_delta: Decimal | None = Field(default=None, gt=Decimal("0"))

    model_config = {"frozen": True}


class TickResult(BaseModel):
    """Resultado de un tick del PositionManager para un símbolo."""

    symbol: str
    trigger: PositionTriggerReason
    mark_price: Decimal
    # client_order_id de la orden de cierre si se disparó un trigger, None si no.
    close_order_id: str | None = None

    model_config = {"frozen": True}
