"""Schemas del PositionManager — configuración y resultados de monitoreo de posiciones."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class PositionTriggerReason(StrEnum):
    NONE = "NONE"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    TRAILING_SL_HIT = "TRAILING_SL_HIT"


class PositionConfig(BaseModel):
    """Configuración de salida para una posición abierta.

    Al menos uno de stop_loss, take_profit o trailing_delta debe estar presente.
    be_trigger_delta: si se setea, mueve el SL a break-even (entry_price) cuando el precio
    se aleja be_trigger_delta unidades a favor. Requiere stop_loss para tener efecto inicial.
    """

    symbol: str
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"))
    # Distancia fija en unidades de precio que el trailing stop mantiene respecto al high-water.
    trailing_delta: Decimal | None = Field(default=None, gt=Decimal("0"))
    # Distancia a favor desde entry_price que activa el movimiento del SL a break-even.
    be_trigger_delta: Decimal | None = Field(default=None, gt=Decimal("0"))

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def at_least_one_trigger(self) -> PositionConfig:
        if self.stop_loss is None and self.take_profit is None and self.trailing_delta is None:
            raise ValueError(
                "At least one of stop_loss, take_profit, or trailing_delta must be set."
            )
        return self


class TickResult(BaseModel):
    """Resultado de un tick del PositionManager para un símbolo."""

    symbol: str
    trigger: PositionTriggerReason
    mark_price: Decimal
    # client_order_id de la orden de cierre si se disparó un trigger, None si no.
    close_order_id: str | None = None

    model_config = {"frozen": True}
