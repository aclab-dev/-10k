"""Unit tests — métricas puras del backtesting (metrics.py).

Cobertura:
- compute_backtest_metrics: sin trades, un trade, múltiples trades.
- win_rate: wins/total, None cuando no hay trades.
- profit_factor: wins/losses, inf cuando solo hay ganadores, None cuando no hay trades.
- expectancy: promedio de net_pnl, None cuando no hay trades.
- max_drawdown: peak-to-trough, 0 cuando todos los trades ganan, None sin trades.
- sharpe_ratio: requiere >= 2 trades, None cuando std=0 o < 2 trades.
- sortino_ratio: requiere >= 2 trades, None cuando no hay downside.
- Reproducibilidad: mismas entradas → mismas salidas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.backtesting.metrics import compute_backtest_metrics
from backend.backtesting.schemas import BacktestRunResult, ClosedTrade

_D = Decimal
_BASE_TS = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    net_pnl: float,
    *,
    gross_pnl: float | None = None,
    entry_fee: float = 0.05,
    exit_fee: float = 0.05,
    entry_slip: float = 0.01,
    exit_slip: float = 0.01,
    funding: float = 0.0,
    side: str = "LONG",
    entry_idx: int = 0,
    exit_idx: int = 1,
) -> ClosedTrade:
    _net = _D(str(net_pnl))
    _gross = (
        _D(str(gross_pnl))
        if gross_pnl is not None
        else (
            _net
            + _D(str(entry_fee))
            + _D(str(exit_fee))
            + _D(str(entry_slip))
            + _D(str(exit_slip))
            + _D(str(funding))
        )
    )
    return ClosedTrade(
        side=side,  # type: ignore[arg-type]
        entry_candle_index=entry_idx,
        exit_candle_index=exit_idx,
        entry_price=_D("100"),
        exit_price=_D("110") if net_pnl > 0 else _D("90"),
        exit_reason="TP" if net_pnl > 0 else "SL",
        leverage=1,
        margin_usdt=_D("10"),
        notional_usdt=_D("10"),
        gross_pnl_usdt=_gross,
        entry_fee_usdt=_D(str(entry_fee)),
        exit_fee_usdt=_D(str(exit_fee)),
        entry_slippage_usdt=_D(str(entry_slip)),
        exit_slippage_usdt=_D(str(exit_slip)),
        funding_cost_usdt=_D(str(funding)),
        net_pnl_usdt=_net,
        hold_candles=exit_idx - entry_idx,
    )


def _run(
    trades: list[ClosedTrade],
    initial_balance: float = 100.0,
) -> BacktestRunResult:
    return compute_backtest_metrics(
        trades=trades,
        symbol="BTCUSDT",
        timeframe="1h",
        candles_processed=len(trades) * 2,
        initial_balance_usdt=_D(str(initial_balance)),
    )


# ---------------------------------------------------------------------------
# Sin trades
# ---------------------------------------------------------------------------


class TestNoTrades:
    def test_returns_backtest_run_result(self) -> None:
        result = _run([])
        assert isinstance(result, BacktestRunResult)

    def test_total_trades_zero(self) -> None:
        assert _run([]).total_trades == 0

    def test_win_rate_none(self) -> None:
        assert _run([]).win_rate is None

    def test_profit_factor_none(self) -> None:
        assert _run([]).profit_factor is None

    def test_expectancy_none(self) -> None:
        assert _run([]).expectancy_usdt is None

    def test_max_drawdown_none(self) -> None:
        assert _run([]).max_drawdown_pct is None

    def test_sharpe_none(self) -> None:
        assert _run([]).sharpe_ratio is None

    def test_sortino_none(self) -> None:
        assert _run([]).sortino_ratio is None

    def test_total_net_pnl_zero(self) -> None:
        assert _run([]).total_net_pnl == _D("0")

    def test_final_balance_equals_initial(self) -> None:
        result = _run([], initial_balance=500.0)
        assert result.final_balance_usdt == _D("500")


# ---------------------------------------------------------------------------
# Un solo trade
# ---------------------------------------------------------------------------


class TestSingleTrade:
    def test_winning_trade_win_rate_one(self) -> None:
        result = _run([_trade(1.0)])
        assert result.win_rate == 1.0
        assert result.winning_trades == 1
        assert result.losing_trades == 0

    def test_losing_trade_win_rate_zero(self) -> None:
        result = _run([_trade(-1.0)])
        assert result.win_rate == 0.0
        assert result.winning_trades == 0
        assert result.losing_trades == 1

    def test_breakeven_trade_counts_as_losing(self) -> None:
        result = _run([_trade(0.0)])
        assert result.losing_trades == 1
        assert result.winning_trades == 0

    def test_sharpe_none_with_one_trade(self) -> None:
        """Sharpe requiere >= 2 trades."""
        assert _run([_trade(1.0)]).sharpe_ratio is None

    def test_sortino_none_with_one_trade(self) -> None:
        assert _run([_trade(1.0)]).sortino_ratio is None

    def test_expectancy_equals_net_pnl(self) -> None:
        result = _run([_trade(2.5)])
        assert result.expectancy_usdt == _D("2.50000000")

    def test_final_balance_updated(self) -> None:
        result = _run([_trade(5.0)], initial_balance=100.0)
        assert result.final_balance_usdt == _D("105.00000000")

    def test_max_drawdown_zero_for_winner(self) -> None:
        result = _run([_trade(1.0)])
        assert result.max_drawdown_pct == 0.0


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------


class TestWinRate:
    def test_two_wins_zero_losses(self) -> None:
        result = _run([_trade(1.0), _trade(2.0)])
        assert result.win_rate == 1.0

    def test_one_win_one_loss(self) -> None:
        result = _run([_trade(1.0), _trade(-1.0)])
        assert result.win_rate == 0.5

    def test_three_trades_two_wins(self) -> None:
        result = _run([_trade(1.0), _trade(1.0), _trade(-1.0)])
        assert abs(result.win_rate - 2 / 3) < 1e-9


# ---------------------------------------------------------------------------
# Profit factor
# ---------------------------------------------------------------------------


class TestProfitFactor:
    def test_only_winners_returns_inf(self) -> None:
        result = _run([_trade(1.0), _trade(2.0)])
        assert result.profit_factor == float("inf")

    def test_only_losers_returns_zero(self) -> None:
        result = _run([_trade(-1.0), _trade(-2.0)])
        assert result.profit_factor == 0.0

    def test_equal_wins_and_losses_returns_one(self) -> None:
        result = _run([_trade(5.0), _trade(-5.0)])
        assert result.profit_factor == 1.0

    def test_two_wins_one_loss(self) -> None:
        result = _run([_trade(4.0), _trade(4.0), _trade(-4.0)])
        assert abs(result.profit_factor - 2.0) < 1e-9

    def test_breakeven_with_winner_yields_inf_profit_factor(self) -> None:
        """Breakeven (net_pnl=0) no aporta a gross_losses; si hay ganadores → inf."""
        result = _run([_trade(1.0), _trade(0.0)])
        # gross_wins = 1, gross_losses = abs(0) = 0 → profit_factor = inf
        assert result.profit_factor == float("inf")

    def test_all_breakeven_trades_yields_none_profit_factor(self) -> None:
        """Cuando gross_wins=0 y gross_losses=0 → None (indefinido)."""
        result = _run([_trade(0.0), _trade(0.0)])
        assert result.profit_factor is None


# ---------------------------------------------------------------------------
# Expectancy
# ---------------------------------------------------------------------------


class TestExpectancy:
    def test_expectancy_average_of_net_pnls(self) -> None:
        result = _run([_trade(3.0), _trade(-1.0)])
        # avg = (3 - 1) / 2 = 1
        assert result.expectancy_usdt == _D("1.00000000")

    def test_expectancy_negative_for_losing_strategy(self) -> None:
        result = _run([_trade(-2.0), _trade(-4.0)])
        assert result.expectancy_usdt < _D("0")

    def test_expectancy_quantized(self) -> None:
        result = _run([_trade(1.0), _trade(2.0), _trade(3.0)])
        assert result.expectancy_usdt == result.expectancy_usdt.quantize(_D("0.00000001"))


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_monotone_winning_strategy_zero_drawdown(self) -> None:
        trades = [_trade(1.0), _trade(1.0), _trade(1.0)]
        assert _run(trades).max_drawdown_pct == 0.0

    def test_single_losing_trade_after_winner(self) -> None:
        # equity: 100 → 110 → 105 ; peak=110, trough=105 → dd = 5/110 ≈ 4.5%
        trades = [_trade(10.0), _trade(-5.0)]
        result = _run(trades, initial_balance=100.0)
        expected = 5.0 / 110.0
        assert abs(result.max_drawdown_pct - expected) < 1e-9

    def test_drawdown_is_peak_to_trough(self) -> None:
        # equity: 100 → 110 → 90 → 95 ; peak=110, trough=90 → dd = 20/110
        trades = [_trade(10.0), _trade(-20.0), _trade(5.0)]
        result = _run(trades, initial_balance=100.0)
        expected = 20.0 / 110.0
        assert abs(result.max_drawdown_pct - expected) < 1e-9

    def test_max_drawdown_non_negative(self) -> None:
        trades = [_trade(-1.0), _trade(-1.0), _trade(-1.0)]
        assert _run(trades).max_drawdown_pct >= 0.0


# ---------------------------------------------------------------------------
# Sharpe ratio
# ---------------------------------------------------------------------------


class TestSharpeRatio:
    def test_none_with_fewer_than_two_trades(self) -> None:
        assert _run([_trade(1.0)]).sharpe_ratio is None

    def test_none_when_all_returns_identical(self) -> None:
        """std = 0 → Sharpe es indefinido."""
        trades = [_trade(1.0), _trade(1.0), _trade(1.0)]
        assert _run(trades).sharpe_ratio is None

    def test_positive_sharpe_for_profitable_strategy(self) -> None:
        trades = [_trade(2.0), _trade(3.0), _trade(2.5)]
        result = _run(trades)
        assert result.sharpe_ratio is not None
        assert result.sharpe_ratio > 0

    def test_negative_sharpe_for_losing_strategy(self) -> None:
        trades = [_trade(-2.0), _trade(-3.0), _trade(-2.5)]
        result = _run(trades)
        assert result.sharpe_ratio is not None
        assert result.sharpe_ratio < 0

    def test_sharpe_computed_on_absolute_pnl(self) -> None:
        """Verificación numérica exacta con 2 trades."""
        import math

        pnls = [2.0, 4.0]
        trades = [_trade(p) for p in pnls]
        mean_r = sum(pnls) / 2
        variance = sum((r - mean_r) ** 2 for r in pnls) / (2 - 1)
        std = math.sqrt(variance)
        expected = mean_r / std

        result = _run(trades)
        assert result.sharpe_ratio is not None
        assert abs(result.sharpe_ratio - expected) < 1e-9


# ---------------------------------------------------------------------------
# Sortino ratio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    def test_none_with_fewer_than_two_trades(self) -> None:
        assert _run([_trade(-1.0)]).sortino_ratio is None

    def test_none_when_no_downside_returns(self) -> None:
        """Sin retornos negativos, downside deviation = 0 → Sortino indefinido."""
        trades = [_trade(1.0), _trade(2.0)]
        assert _run(trades).sortino_ratio is None

    def test_positive_sortino_for_profitable_strategy_with_losses(self) -> None:
        trades = [_trade(5.0), _trade(-1.0), _trade(4.0)]
        result = _run(trades)
        assert result.sortino_ratio is not None
        assert result.sortino_ratio > 0

    def test_sortino_computed_only_on_downside(self) -> None:
        """Verificación: sortino usa solo los retornos negativos en el denominador."""
        import math

        pnls = [2.0, -1.0, 3.0]
        trades = [_trade(p) for p in pnls]
        mean_r = sum(pnls) / len(pnls)
        downside_sq = [(r - 0.0) ** 2 for r in pnls if r < 0.0]
        downside_std = math.sqrt(sum(downside_sq) / len(downside_sq))
        expected = mean_r / downside_std

        result = _run(trades)
        assert result.sortino_ratio is not None
        assert abs(result.sortino_ratio - expected) < 1e-9


# ---------------------------------------------------------------------------
# Aggregated cost fields
# ---------------------------------------------------------------------------


class TestAggregatedCosts:
    def test_total_fees_is_sum_of_entry_and_exit_fees(self) -> None:
        t1 = _trade(1.0, entry_fee=0.05, exit_fee=0.05)
        t2 = _trade(2.0, entry_fee=0.10, exit_fee=0.10)
        result = _run([t1, t2])
        assert result.total_fees_paid == _D("0.30000000")

    def test_total_slippage_is_sum_of_entry_and_exit_slippage(self) -> None:
        t1 = _trade(1.0, entry_slip=0.01, exit_slip=0.01)
        t2 = _trade(2.0, entry_slip=0.02, exit_slip=0.02)
        result = _run([t1, t2])
        assert result.total_slippage_cost == _D("0.06000000")

    def test_total_funding_is_sum_of_funding_costs(self) -> None:
        t1 = _trade(1.0, funding=0.10)
        t2 = _trade(2.0, funding=0.20)
        result = _run([t1, t2])
        assert result.total_funding_cost == _D("0.30000000")

    def test_total_net_pnl_is_sum_of_net_pnls(self) -> None:
        result = _run([_trade(3.0), _trade(-1.0)])
        assert result.total_net_pnl == _D("2.00000000")

    def test_final_balance_is_initial_plus_net_pnl(self) -> None:
        result = _run([_trade(5.0), _trade(-2.0)], initial_balance=100.0)
        assert result.final_balance_usdt == _D("103.00000000")


# ---------------------------------------------------------------------------
# Reproducibilidad
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_same_trades_same_result(self) -> None:
        trades = [_trade(1.0), _trade(-0.5), _trade(2.0)]
        r1 = _run(trades)
        r2 = _run(trades)
        assert r1.total_net_pnl == r2.total_net_pnl
        assert r1.sharpe_ratio == r2.sharpe_ratio
        assert r1.sortino_ratio == r2.sortino_ratio
        assert r1.max_drawdown_pct == r2.max_drawdown_pct
        assert r1.win_rate == r2.win_rate

    def test_metadata_preserved(self) -> None:
        result = compute_backtest_metrics(
            trades=[],
            symbol="ETHUSDT",
            timeframe="4h",
            candles_processed=200,
            initial_balance_usdt=_D("500"),
        )
        assert result.symbol == "ETHUSDT"
        assert result.timeframe == "4h"
        assert result.candles_processed == 200
