"""Fee model for PAPER mode fill simulation.

Default rates reflect USDT-M perpetual futures (conservative baseline):
  - Taker (MARKET / STOP): 0.05 %
  - Maker (LIMIT)         : 0.02 %
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

OrderType = Literal["MARKET", "LIMIT", "STOP"]

DEFAULT_TAKER_RATE = Decimal("0.0005")
DEFAULT_MAKER_RATE = Decimal("0.0002")
_QUANT = Decimal("0.00000001")

_TAKER_TYPES: frozenset[str] = frozenset({"MARKET", "STOP"})
_VALID_ORDER_TYPES: frozenset[str] = frozenset({"MARKET", "LIMIT", "STOP"})


class FeeModel:
    """Calculates trading fee for a single simulated fill.

    Args:
        taker_rate: fee rate for taker orders (MARKET/STOP). Default: 0.05%.
        maker_rate: fee rate for maker orders (LIMIT). Default: 0.02%.
    """

    def __init__(
        self,
        taker_rate: Decimal = DEFAULT_TAKER_RATE,
        maker_rate: Decimal = DEFAULT_MAKER_RATE,
    ) -> None:
        self._taker_rate = taker_rate
        self._maker_rate = maker_rate

    def calculate(self, notional_usdt: Decimal, order_type: OrderType) -> Decimal:
        """Return fee in USDT for *notional_usdt* at the given *order_type*.

        Args:
            notional_usdt: fill_price × filled_quantity
            order_type: "MARKET", "LIMIT", or "STOP"

        Returns:
            Fee in USDT, quantized to 8 decimal places.

        Raises:
            ValueError: if *order_type* is not a recognised value.
        """
        if order_type not in _VALID_ORDER_TYPES:
            raise ValueError(
                f"Unknown order_type: {order_type!r}. Expected one of {sorted(_VALID_ORDER_TYPES)}"
            )
        rate = self._taker_rate if order_type in _TAKER_TYPES else self._maker_rate
        return (notional_usdt * rate).quantize(_QUANT)
