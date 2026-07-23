"""Schemas del PositionManager — configuración y resultados de monitoreo de posiciones."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class PositionTriggerReason(StrEnum):
    NONE = "NONE"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    TP_PARTIAL = "TP_PARTIAL"  # cierre parcial en un nivel de multi-TP
    TRAILING_SL_HIT = "TRAILING_SL_HIT"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"  # acción por invalidación de setup


class TakeProfitLevel(BaseModel):
    """Un nivel de take profit parcial para soporte multi-TP.

    close_fraction: fracción de la cantidad *remanente* de la posición al momento
    del tick en que se dispara este nivel (no de la cantidad original). Para cerrar
    todo en el último nivel, usar close_fraction=1. Ejemplo: dos niveles con
    close_fraction=0.5 y close_fraction=1 cierran 50% en el primero y 100% del
    remanente (otro 50% original) en el segundo.

    El caller es responsable de que los niveles estén ordenados correctamente
    (ascendente para LONG, descendente para SHORT).
    """

    price: Decimal = Field(gt=Decimal("0"))
    close_fraction: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))

    model_config = {"frozen": True}


class InvalidationAction(BaseModel):
    """Acción a ejecutar cuando el setup que originó la posición se invalida.

    Al menos uno de new_sl o close_fraction > 0 debe estar presente.
    new_sl: nuevo SL efectivo a aplicar (None = no cambiar).
    close_fraction: fracción de la posición a cerrar inmediatamente (0 = no cerrar).
    """

    new_sl: Decimal | None = Field(default=None, gt=Decimal("0"))
    close_fraction: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def at_least_one_action(self) -> InvalidationAction:
        if self.new_sl is None and self.close_fraction == Decimal("0"):
            raise ValueError(
                "InvalidationAction must specify at least new_sl or close_fraction > 0."
            )
        return self


class PositionConfig(BaseModel):
    """Configuración de salida para una posición abierta.

    take_profit y take_profit_levels son mutuamente excluyentes.
    Al menos uno de stop_loss, take_profit, take_profit_levels o trailing_delta debe estar presente.

    be_trigger_delta: si se setea, mueve el SL efectivo a entry_price + be_sl_offset
    (LONG) o entry_price - be_sl_offset (SHORT) cuando el precio se aleja
    be_trigger_delta unidades a favor. be_sl_offset=0 (default) mueve exactamente a entry.

    invalidation_action: acción a aplicar al llamar trigger_setup_invalidation().

    Nota de diseño: no se valida que stop_loss/take_profit sean coherentes con el lado
    de la posición porque entry_price no se conoce en el momento de construcción.
    La validación contextual es responsabilidad del caller.
    """

    symbol: str
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"))
    take_profit_levels: list[TakeProfitLevel] = Field(default_factory=list)
    trailing_delta: Decimal | None = Field(default=None, gt=Decimal("0"))
    be_trigger_delta: Decimal | None = Field(default=None, gt=Decimal("0"))
    be_sl_offset: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    invalidation_action: InvalidationAction | None = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def validate_config(self) -> PositionConfig:
        # take_profit y take_profit_levels son mutuamente excluyentes
        if self.take_profit is not None and self.take_profit_levels:
            raise ValueError("take_profit and take_profit_levels are mutually exclusive.")

        # Al menos un mecanismo de salida debe estar presente
        has_exit = (
            self.stop_loss is not None
            or self.take_profit is not None
            or bool(self.take_profit_levels)
            or self.trailing_delta is not None
        )
        if not has_exit:
            raise ValueError(
                "At least one of stop_loss, take_profit, take_profit_levels,"
                " or trailing_delta must be set."
            )

        # be_sl_offset solo tiene efecto cuando be_trigger_delta está configurado
        if self.be_sl_offset > Decimal("0") and self.be_trigger_delta is None:
            raise ValueError(
                "be_sl_offset has no effect when be_trigger_delta is None."
            )

        # Si be_sl_offset >= be_trigger_delta, el SL queda al nivel del trigger o por
        # encima del mark en el mismo tick que activa el break-even, cerrando la posición.
        if (
            self.be_trigger_delta is not None
            and self.be_sl_offset >= self.be_trigger_delta
        ):
            raise ValueError(
                "be_sl_offset must be less than be_trigger_delta; otherwise the SL"
                " lands at or beyond the trigger price and closes the position"
                " immediately on activation."
            )

        return self


class TickResult(BaseModel):
    """Resultado de un tick del PositionManager para un símbolo."""

    symbol: str
    trigger: PositionTriggerReason
    mark_price: Decimal
    close_order_id: str | None = None
    # Índice del nivel de TP disparado (0-based, solo en multi-TP)
    tp_level_index: int | None = None
    # Fracción de la posición cerrada en este tick:
    # TP_PARTIAL, TP_HIT de multi-TP, y SETUP_INVALIDATED con cierre parcial/total.
    closed_fraction: Decimal | None = None

    model_config = {"frozen": True}
