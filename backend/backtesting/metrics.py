"""Backtesting session metrics aggregator.

Computes aggregate cost and fill statistics from a list of OrderResult
objects produced by the PaperAdapter during a backtesting run.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.exchange_adapters.schemas import OrderResult, OrderStatus

_QUANT = Decimal("0.00000001")


@dataclass(frozen=True)
class BacktestingMetrics:
    """Aggregate metrics for a completed backtesting session."""

    total_trades: int
    full_fills: int
    partial_fills: int
    total_fees_usdt: Decimal
    total_slippage_usdt: Decimal
    total_funding_usdt: Decimal
    gross_pnl_usdt: Decimal
    net_pnl_usdt: Decimal
    win_count: int
    loss_count: int
    win_rate: float | None

    @classmethod
    def empty(cls) -> "BacktestingMetrics":
        return cls(
            total_trades=0,
            full_fills=0,
            partial_fills=0,
            total_fees_usdt=Decimal("0"),
            total_slippage_usdt=Decimal("0"),
            total_funding_usdt=Decimal("0"),
            gross_pnl_usdt=Decimal("0"),
            net_pnl_usdt=Decimal("0"),
            win_count=0,
            loss_count=0,
            win_rate=None,
        )


def compute_backtesting_metrics(
    results: list[OrderResult],
    realized_pnl_per_trade: list[Decimal] | None = None,
    total_funding_usdt: Decimal = Decimal("0"),
) -> BacktestingMetrics:
    """Compute aggregate metrics from a list of OrderResult objects.

    Only FILLED and PARTIALLY_FILLED orders are counted as trades.

    Args:
        results: list of OrderResult from the PaperAdapter.
        realized_pnl_per_trade: optional per-trade gross PnL in USDT. Must
            have the same length as the filled subset of *results*.
        total_funding_usdt: total funding paid/received for the session
            (positive = we paid, negative = we received).

    Raises:
        ValueError: if *realized_pnl_per_trade* length mismatches filled trades.
    """
    filled_statuses = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
    filled = [r for r in results if r.status in filled_statuses]

    if not filled:
        return BacktestingMetrics.empty()

    if realized_pnl_per_trade is not None and len(realized_pnl_per_trade) != len(filled):
        raise ValueError(
            f"realized_pnl_per_trade has {len(realized_pnl_per_trade)} entries "
            f"but there are {len(filled)} filled trades."
        )

    total_fees = sum((r.fee_usdt for r in filled), Decimal("0")).quantize(_QUANT)
    total_slippage = sum((r.slippage_usdt for r in filled), Decimal("0")).quantize(_QUANT)

    full_fills = sum(1 for r in filled if r.quantity_filled >= r.quantity_requested)
    partial_fills = len(filled) - full_fills

    if realized_pnl_per_trade is not None:
        gross_pnl = sum(realized_pnl_per_trade, Decimal("0")).quantize(_QUANT)
        win_count = sum(1 for p in realized_pnl_per_trade if p > Decimal("0"))
        loss_count = sum(1 for p in realized_pnl_per_trade if p <= Decimal("0"))
        win_rate: float | None = win_count / len(realized_pnl_per_trade)
    else:
        gross_pnl = Decimal("0")
        win_count = 0
        loss_count = 0
        win_rate = None

    funding = total_funding_usdt.quantize(_QUANT)
    net_pnl = (gross_pnl - total_fees - total_slippage - funding).quantize(_QUANT)

    return BacktestingMetrics(
        total_trades=len(filled),
        full_fills=full_fills,
        partial_fills=partial_fills,
        total_fees_usdt=total_fees,
        total_slippage_usdt=total_slippage,
        total_funding_usdt=funding,
        gross_pnl_usdt=gross_pnl,
        net_pnl_usdt=net_pnl,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
    )
