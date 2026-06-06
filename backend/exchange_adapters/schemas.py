"""Schemas del ExchangeAdapter — contratos de entrada/salida para operaciones de exchange.

Estos schemas son el contrato entre el Execution Engine y cualquier adapter
(PaperAdapter, BingXAdapter, etc.). Son inmutables una vez construidos.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from backend.market_data.schemas import ALLOWED_SYMBOLS

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# OrderRequest
# ---------------------------------------------------------------------------


class OrderRequest(BaseModel):
    """Solicitud de orden enviada al ExchangeAdapter.

    client_order_id es el identificador de idempotencia: el adapter debe
    retornar el resultado previo si ya existe una orden con ese id.
    """

    client_order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(gt=Decimal("0"))
    price: Decimal | None = Field(default=None, gt=Decimal("0"))
    stop_price: Decimal | None = Field(default=None, ge=Decimal("0"))
    is_reduce_only: bool = False

    model_config = {"frozen": True}

    @field_validator("client_order_id")
    @classmethod
    def must_be_valid_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"'{v}' no es un UUID válido.") from exc
        return v

    @field_validator("symbol")
    @classmethod
    def symbol_allowed(cls, v: str) -> str:
        if v not in ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(ALLOWED_SYMBOLS)}")
        return v


# ---------------------------------------------------------------------------
# OrderResult
# ---------------------------------------------------------------------------


class OrderResult(BaseModel):
    """Resultado de una operación de orden devuelto por el ExchangeAdapter."""

    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity_requested: Decimal
    quantity_filled: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    fill_price: Decimal | None = Field(default=None, ge=Decimal("0"))
    fee_usdt: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    slippage_usdt: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    is_simulated: bool
    timestamp_utc: datetime
    error: str | None = None

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# PositionState
# ---------------------------------------------------------------------------


class PositionState(BaseModel):
    """Estado de una posición abierta según el exchange (real o simulado)."""

    position_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(gt=Decimal("0"))
    entry_price: Decimal = Field(gt=Decimal("0"))
    mark_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    unrealized_pnl: Decimal = Field(default=Decimal("0"))
    margin_usdt: Decimal = Field(gt=Decimal("0"))
    leverage: int = Field(ge=1)  # cap por entorno vive en el adapter, no en el schema
    stop_loss: Decimal | None = Field(default=None, ge=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, ge=Decimal("0"))
    is_simulated: bool

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# AccountState
# ---------------------------------------------------------------------------


class AccountState(BaseModel):
    """Estado de la cuenta según el exchange (real o simulado)."""

    balance_usdt: Decimal = Field(ge=Decimal("0"))
    equity_usdt: Decimal = Field(ge=Decimal("0"))
    available_margin_usdt: Decimal = Field(ge=Decimal("0"))
    used_margin_usdt: Decimal = Field(ge=Decimal("0"))
    is_simulated: bool

    model_config = {"frozen": True}
