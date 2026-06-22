"""Latency model for PAPER / backtesting fill simulation.

Models the price impact of execution latency: the time between signal
emission and actual order fill.  Converts latency in milliseconds to
extra adverse BPS applied on top of slippage.

Default: 50 ms at 0.002 BPS/ms ≈ 0.1 BPS extra adverse movement.
This is a conservative baseline for a low-latency co-located setup;
increase `latency_ms` to reflect real-world WAN round-trips (~200 ms).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]

_DEFAULT_LATENCY_MS: int = 50
_DEFAULT_BPS_PER_MS: Decimal = Decimal("0.002")
_BASIS: Decimal = Decimal("10000")
_QUANT: Decimal = Decimal("0.00000001")

_VALID_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})


class LatencyModel:
    """Applies latency-based price impact to a reference fill price.

    The model treats latency as additional adverse slippage: during the
    latency window the market can move against the order.  BUY orders
    pay more; SELL orders receive less.

    Args:
        latency_ms: execution latency in milliseconds. Default: 50 ms.
        bps_per_ms: price drift per millisecond in basis points.
                    Default: 0.002 BPS/ms.
    """

    def __init__(
        self,
        latency_ms: int = _DEFAULT_LATENCY_MS,
        bps_per_ms: Decimal = _DEFAULT_BPS_PER_MS,
    ) -> None:
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {latency_ms}")
        if bps_per_ms < Decimal("0"):
            raise ValueError(f"bps_per_ms must be >= 0, got {bps_per_ms}")
        self._latency_ms = latency_ms
        self._bps_per_ms = bps_per_ms

    @property
    def latency_ms(self) -> int:
        return self._latency_ms

    @property
    def bps_per_ms(self) -> Decimal:
        return self._bps_per_ms

    def apply(self, price: Decimal, side: Side) -> Decimal:
        """Return the fill price after latency-based price impact.

        BUY  → price × (1 + total_bps / 10 000)  [pays more]
        SELL → price × (1 − total_bps / 10 000)  [receives less]

        When latency_ms == 0 the price is returned unchanged (quantized).

        Args:
            price: reference fill price in USDT.
            side: "BUY" or "SELL".

        Returns:
            Adjusted fill price quantized to 8 decimal places.

        Raises:
            ValueError: if *side* is not recognised.
        """
        if side not in _VALID_SIDES:
            raise ValueError(f"Unknown side: {side!r}. Expected one of {sorted(_VALID_SIDES)}")

        if self._latency_ms == 0:
            return price.quantize(_QUANT)

        total_bps = self._bps_per_ms * Decimal(self._latency_ms)
        factor = total_bps / _BASIS

        if side == "BUY":
            return (price * (1 + factor)).quantize(_QUANT)
        return (price * (1 - factor)).quantize(_QUANT)

    def latency_cost_usdt(self, price: Decimal, quantity: Decimal, side: Side) -> Decimal:
        """Return the USDT cost attributable to latency for a given fill.

        This is the absolute difference between the latency-adjusted price
        and the reference price, multiplied by quantity.

        Args:
            price: reference fill price in USDT.
            quantity: filled quantity.
            side: "BUY" or "SELL".

        Returns:
            Latency cost in USDT, quantized to 8 decimal places.
        """
        adjusted = self.apply(price, side)
        return (abs(adjusted - price) * quantity).quantize(_QUANT)
