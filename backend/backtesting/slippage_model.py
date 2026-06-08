"""Slippage model for PAPER mode fill simulation.

Default approach — fixed BPS:
  - MARKET / STOP: 2 BPS (0.02 %) applied adversely — BUY pays more, SELL receives less.
  - LIMIT         : fills at stated price, no slippage.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]

_DEFAULT_MARKET_BPS = Decimal("2")
_BASIS = Decimal("10000")
_QUANT = Decimal("0.00000001")

_VALID_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})
_VALID_ORDER_TYPES: frozenset[str] = frozenset({"MARKET", "LIMIT", "STOP"})


class SlippageModel:
    """Applies fixed-BPS slippage to a reference price for PAPER fills.

    Args:
        market_bps: slippage in basis points for MARKET/STOP orders. Default: 2 BPS.
    """

    def __init__(self, market_bps: Decimal = _DEFAULT_MARKET_BPS) -> None:
        self._market_bps = market_bps

    def apply(self, price: Decimal, side: Side, order_type: str) -> Decimal:
        """Return the fill price after slippage.

        MARKET / STOP orders are nudged adversely:
          - BUY  → price × (1 + bps / 10 000)
          - SELL → price × (1 − bps / 10 000)

        LIMIT orders always fill at *price*.

        Args:
            price: reference last-trade price in USDT
            side: "BUY" or "SELL"
            order_type: "MARKET", "LIMIT", or "STOP"

        Returns:
            Fill price quantized to 8 decimal places.

        Raises:
            ValueError: if *side* or *order_type* is not a recognised value.
        """
        if side not in _VALID_SIDES:
            raise ValueError(f"Unknown side: {side!r}. Expected one of {sorted(_VALID_SIDES)}")
        if order_type not in _VALID_ORDER_TYPES:
            raise ValueError(
                f"Unknown order_type: {order_type!r}. Expected one of {sorted(_VALID_ORDER_TYPES)}"
            )

        if order_type == "LIMIT":
            return price.quantize(_QUANT)

        factor = self._market_bps / _BASIS
        if side == "BUY":
            return (price * (1 + factor)).quantize(_QUANT)
        return (price * (1 - factor)).quantize(_QUANT)
