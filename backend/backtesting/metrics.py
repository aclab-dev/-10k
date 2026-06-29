"""Cálculo de métricas de performance para el Backtesting Engine (F12 [90]).

Todas las funciones son puras: misma lista de ClosedTrade → mismos valores.
No hay estado ni side-effects.
"""

from __future__ import annotations

import math
from decimal import Decimal

from backend.backtesting.constants import QUANT as _QUANT
from backend.backtesting.schemas import BacktestRunResult, ClosedTrade

_ZERO = Decimal("0")
_RISK_FREE_RATE = 0.0  # asumimos 0 para sharpe/sortino en crypto


def compute_backtest_metrics(
    trades: list[ClosedTrade],
    symbol: str,
    timeframe: str,
    candles_processed: int,
    initial_balance_usdt: Decimal,
) -> BacktestRunResult:
    """Agrega la lista de trades en un BacktestRunResult con métricas completas.

    Args:
        trades: lista de trades cerrados en orden cronológico.
        symbol: símbolo del instrumento (solo metadata).
        timeframe: timeframe de los candles (solo metadata).
        candles_processed: cantidad de candles procesados.
        initial_balance_usdt: balance inicial de la simulación.

    Returns:
        BacktestRunResult con todas las métricas calculadas.
    """
    total = len(trades)
    winning = sum(1 for t in trades if t.net_pnl_usdt > _ZERO)
    # Breakeven (net_pnl == 0) cuenta como losing: clasificación conservadora
    # coherente con profit_factor (breakeven no aporta al numerador).
    losing = sum(1 for t in trades if t.net_pnl_usdt <= _ZERO)

    gross_pnl = sum((t.gross_pnl_usdt for t in trades), _ZERO)
    fees = sum((t.entry_fee_usdt + t.exit_fee_usdt for t in trades), _ZERO)
    slippage = sum((t.entry_slippage_usdt + t.exit_slippage_usdt for t in trades), _ZERO)
    funding = sum((t.funding_cost_usdt for t in trades), _ZERO)
    net_pnl = sum((t.net_pnl_usdt for t in trades), _ZERO)

    win_rate = (winning / total) if total > 0 else None
    profit_factor = _profit_factor(trades)
    expectancy = _expectancy(trades)
    max_dd = _max_drawdown(trades, initial_balance_usdt)
    sharpe = _sharpe_ratio(trades)
    sortino = _sortino_ratio(trades)

    return BacktestRunResult(
        symbol=symbol,
        timeframe=timeframe,
        candles_processed=candles_processed,
        trades=trades,
        total_trades=total,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_usdt=expectancy,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        total_gross_pnl=gross_pnl.quantize(_QUANT),
        total_fees_paid=fees.quantize(_QUANT),
        total_slippage_cost=slippage.quantize(_QUANT),
        total_funding_cost=funding.quantize(_QUANT),
        total_net_pnl=net_pnl.quantize(_QUANT),
        final_balance_usdt=(initial_balance_usdt + net_pnl).quantize(_QUANT),
    )


def _profit_factor(trades: list[ClosedTrade]) -> float | None:
    gross_wins = sum(float(t.net_pnl_usdt) for t in trades if t.net_pnl_usdt > _ZERO)
    gross_losses = sum(abs(float(t.net_pnl_usdt)) for t in trades if t.net_pnl_usdt <= _ZERO)
    if gross_losses == 0:
        return None if gross_wins == 0 else float("inf")
    return gross_wins / gross_losses


def _expectancy(trades: list[ClosedTrade]) -> Decimal | None:
    if not trades:
        return None
    total = sum((t.net_pnl_usdt for t in trades), _ZERO)
    return (total / Decimal(len(trades))).quantize(_QUANT)


def _max_drawdown(trades: list[ClosedTrade], initial_balance: Decimal) -> float | None:
    """Peak-to-trough max drawdown sobre el equity acumulado."""
    if not trades:
        return None

    equity = initial_balance
    peak = equity
    max_dd = 0.0

    for trade in trades:
        equity += trade.net_pnl_usdt
        if equity > peak:
            peak = equity
        drawdown = float((peak - equity) / peak) if peak > _ZERO else 0.0
        if drawdown > max_dd:
            max_dd = drawdown

    return max_dd


def _sharpe_ratio(trades: list[ClosedTrade]) -> float | None:
    """Sharpe ratio calculado sobre PnL absoluto en USDT (no sobre retornos porcentuales).

    Nota: solo es comparable entre runs con el mismo margin_usdt. Sin anualización.
    """
    if len(trades) < 2:
        return None

    returns = [float(t.net_pnl_usdt) for t in trades]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)

    if std == 0:
        return None
    return (mean_r - _RISK_FREE_RATE) / std


def _sortino_ratio(trades: list[ClosedTrade]) -> float | None:
    """Sortino ratio calculado sobre PnL absoluto en USDT (no sobre retornos porcentuales).

    Nota: solo es comparable entre runs con el mismo margin_usdt. Sin anualización.
    """
    if len(trades) < 2:
        return None

    returns = [float(t.net_pnl_usdt) for t in trades]
    mean_r = sum(returns) / len(returns)
    downside_sq = [(r - _RISK_FREE_RATE) ** 2 for r in returns if r < _RISK_FREE_RATE]

    if not downside_sq:
        return None
    downside_std = math.sqrt(sum(downside_sq) / len(downside_sq))

    if downside_std == 0:
        return None
    return (mean_r - _RISK_FREE_RATE) / downside_std
