"""Partial fill model for PAPER / backtesting fill simulation.

Models the scenario where insufficient liquidity prevents an order from
being fully filled.  In live markets, large orders on thin books may
only execute a fraction of the requested quantity.

Default: `fill_ratio = 1.0` → always full fill (conservative paper baseline).
Set `fill_ratio < 1.0` to simulate illiquid conditions (e.g. 0.8 = 80% fill).
"""

from __future__ import annotations

from decimal import Decimal

from backend.backtesting.constants import QUANT as _QUANT

_DEFAULT_FILL_RATIO: Decimal = Decimal("1.0")


class PartialFillModel:
    """Simulates partial order execution due to limited market liquidity.

    The model applies a deterministic fill ratio to the requested quantity.
    A ratio of 1.0 reproduces the full-fill behaviour of the basic paper
    adapter; ratios below 1.0 simulate thin-book conditions.

    Args:
        fill_ratio: fraction of the order that gets filled, in [0, 1].
                    Default: 1.0 (always full fill).
    """

    def __init__(self, fill_ratio: Decimal = _DEFAULT_FILL_RATIO) -> None:
        if not (Decimal("0") <= fill_ratio <= Decimal("1")):
            raise ValueError(f"fill_ratio must be in [0, 1], got {fill_ratio}")
        self._fill_ratio = fill_ratio

    @property
    def fill_ratio(self) -> Decimal:
        return self._fill_ratio

    def compute(self, requested_quantity: Decimal) -> Decimal:
        """Return the filled quantity for a given *requested_quantity*.

        Args:
            requested_quantity: order size requested.

        Returns:
            Filled quantity (≤ requested_quantity), quantized to 8 decimal places.

        Raises:
            ValueError: if *requested_quantity* is negative.
        """
        if requested_quantity < Decimal("0"):
            raise ValueError(f"requested_quantity must be >= 0, got {requested_quantity}")
        return (requested_quantity * self._fill_ratio).quantize(_QUANT)

    def is_partial(self, requested_quantity: Decimal) -> bool:
        """Return True if the fill will be partial (filled < requested).

        Args:
            requested_quantity: order size requested.
        """
        return self._fill_ratio < Decimal("1")
