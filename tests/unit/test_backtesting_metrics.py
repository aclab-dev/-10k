"""Unit tests — BacktestingMetrics + compute_backtesting_metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.backtesting.metrics import BacktestingMetrics, compute_backtesting_metrics
from backend.exchange_adapters.schemas import (
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _make_result(
    *,
    status: OrderStatus = OrderStatus.FILLED,
    quantity_requested: Decimal = Decimal("0.001"),
    quantity_filled: Decimal = Decimal("0.001"),
    fee_usdt: Decimal = Decimal("0.05"),
    slippage_usdt: Decimal = Decimal("0.02"),
) -> OrderResult:
    return OrderResult(
        client_order_id="00000000-0000-0000-0000-000000000001",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=status,
        quantity_requested=quantity_requested,
        quantity_filled=quantity_filled,
        fill_price=Decimal("97000"),
        fee_usdt=fee_usdt,
        slippage_usdt=slippage_usdt,
        is_simulated=True,
        timestamp_utc=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Empty / no filled trades
# ---------------------------------------------------------------------------


class TestComputeBacktestingMetricsEmpty:
    def test_empty_list_returns_zero_metrics(self) -> None:
        m = compute_backtesting_metrics([])
        assert m == BacktestingMetrics.empty()

    def test_all_pending_returns_zero_metrics(self) -> None:
        results = [_make_result(status=OrderStatus.PENDING)]
        m = compute_backtesting_metrics(results)
        assert m == BacktestingMetrics.empty()

    def test_cancelled_orders_excluded(self) -> None:
        results = [_make_result(status=OrderStatus.CANCELLED)]
        m = compute_backtesting_metrics(results)
        assert m.total_trades == 0

    def test_failed_orders_excluded(self) -> None:
        results = [_make_result(status=OrderStatus.FAILED)]
        m = compute_backtesting_metrics(results)
        assert m.total_trades == 0


# ---------------------------------------------------------------------------
# Fill counting
# ---------------------------------------------------------------------------


class TestFillCounting:
    def test_single_full_fill(self) -> None:
        m = compute_backtesting_metrics([_make_result()])
        assert m.total_trades == 1
        assert m.full_fills == 1
        assert m.partial_fills == 0

    def test_partial_fill_detected(self) -> None:
        result = _make_result(
            status=OrderStatus.PARTIALLY_FILLED,
            quantity_requested=Decimal("1.0"),
            quantity_filled=Decimal("0.8"),
        )
        m = compute_backtesting_metrics([result])
        assert m.total_trades == 1
        assert m.partial_fills == 1
        assert m.full_fills == 0

    def test_mixed_full_and_partial(self) -> None:
        full = _make_result()
        partial = _make_result(
            status=OrderStatus.PARTIALLY_FILLED,
            quantity_requested=Decimal("1.0"),
            quantity_filled=Decimal("0.5"),
        )
        m = compute_backtesting_metrics([full, partial])
        assert m.total_trades == 2
        assert m.full_fills == 1
        assert m.partial_fills == 1


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------


class TestCostAggregation:
    def test_fees_summed_correctly(self) -> None:
        r1 = _make_result(fee_usdt=Decimal("0.05"))
        r2 = _make_result(fee_usdt=Decimal("0.03"))
        m = compute_backtesting_metrics([r1, r2])
        assert m.total_fees_usdt == Decimal("0.08000000")

    def test_slippage_summed_correctly(self) -> None:
        r1 = _make_result(slippage_usdt=Decimal("0.10"))
        r2 = _make_result(slippage_usdt=Decimal("0.05"))
        m = compute_backtesting_metrics([r1, r2])
        assert m.total_slippage_usdt == Decimal("0.15000000")

    def test_funding_passed_through(self) -> None:
        m = compute_backtesting_metrics([_make_result()], total_funding_usdt=Decimal("2.5"))
        assert m.total_funding_usdt == Decimal("2.50000000")

    def test_net_pnl_deducts_all_costs(self) -> None:
        result = _make_result(fee_usdt=Decimal("0.05"), slippage_usdt=Decimal("0.02"))
        pnl = [Decimal("10")]
        m = compute_backtesting_metrics(
            [result], realized_pnl_per_trade=pnl, total_funding_usdt=Decimal("1")
        )
        # net = 10 - 0.05 - 0.02 - 1 = 8.93
        assert m.net_pnl_usdt == Decimal("8.93000000")


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------


class TestWinRate:
    def test_win_rate_none_without_pnl(self) -> None:
        m = compute_backtesting_metrics([_make_result()])
        assert m.win_rate is None

    def test_win_rate_one_when_all_wins(self) -> None:
        results = [_make_result(), _make_result()]
        pnl = [Decimal("5"), Decimal("3")]
        m = compute_backtesting_metrics(results, realized_pnl_per_trade=pnl)
        assert m.win_rate == pytest.approx(1.0)
        assert m.win_count == 2
        assert m.loss_count == 0

    def test_win_rate_zero_when_all_losses(self) -> None:
        results = [_make_result(), _make_result()]
        pnl = [Decimal("-5"), Decimal("-3")]
        m = compute_backtesting_metrics(results, realized_pnl_per_trade=pnl)
        assert m.win_rate == pytest.approx(0.0)
        assert m.win_count == 0
        assert m.loss_count == 2

    def test_win_rate_fifty_percent(self) -> None:
        results = [_make_result(), _make_result()]
        pnl = [Decimal("5"), Decimal("-3")]
        m = compute_backtesting_metrics(results, realized_pnl_per_trade=pnl)
        assert m.win_rate == pytest.approx(0.5)

    def test_break_even_counts_as_loss(self) -> None:
        results = [_make_result()]
        pnl = [Decimal("0")]
        m = compute_backtesting_metrics(results, realized_pnl_per_trade=pnl)
        assert m.win_count == 0
        assert m.loss_count == 1

    def test_pnl_length_mismatch_raises(self) -> None:
        results = [_make_result(), _make_result()]
        with pytest.raises(ValueError, match="realized_pnl_per_trade"):
            compute_backtesting_metrics(results, realized_pnl_per_trade=[Decimal("1")])
