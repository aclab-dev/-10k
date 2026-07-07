"""Tests unitarios del BacktestService (F12 [94]).

Cobertura:
- run_and_persist crea BacktestRun + BacktestResult con métricas correctas.
- Todas las métricas del DoD se persisten: Sharpe, Sortino, max drawdown,
  expectancy, hit ratio (win_rate), profit_factor.
- Un engine que lanza excepción deja BacktestRun con status FAILED.
- BacktestResult refleja correctamente best/worst trade pnl.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backend.backtesting.schemas import (
    BacktestConfig,
    BacktestRunResult,
    CandleRow,
    ClosedTrade,
)
from backend.backtesting.service import BacktestService
from backend.storage.models import BacktestRun

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 1, 1, tzinfo=UTC)
_D = Decimal


def _candle(close: float, *, offset_hours: int = 0) -> CandleRow:
    p = _D(str(close))
    return CandleRow(
        timestamp_utc=_BASE_TS + timedelta(hours=offset_hours),
        open=p,
        high=p + _D("1"),
        low=p - _D("1"),
        close=p,
        volume=_D("1000"),
    )


def _trade(net_pnl: float, *, gross_pnl: float | None = None) -> ClosedTrade:
    gp = _D(str(gross_pnl)) if gross_pnl is not None else _D(str(net_pnl))
    np_ = _D(str(net_pnl))
    return ClosedTrade(
        side="LONG",
        entry_candle_index=0,
        exit_candle_index=1,
        entry_price=_D("100"),
        exit_price=_D("110"),
        exit_reason="TP",
        leverage=1,
        margin_usdt=_D("10"),
        notional_usdt=_D("10"),
        gross_pnl_usdt=gp,
        entry_fee_usdt=_D("0"),
        exit_fee_usdt=_D("0"),
        entry_slippage_usdt=_D("0"),
        exit_slippage_usdt=_D("0"),
        funding_cost_usdt=_D("0"),
        net_pnl_usdt=np_,
        hold_candles=1,
    )


def _mock_result(trades: list[ClosedTrade]) -> BacktestRunResult:
    net = sum(t.net_pnl_usdt for t in trades)
    wins = sum(1 for t in trades if t.net_pnl_usdt > 0)
    losses = len(trades) - wins
    return BacktestRunResult(
        symbol="BTCUSDT",
        timeframe="1h",
        candles_processed=len(trades) + 1,
        trades=trades,
        total_trades=len(trades),
        winning_trades=wins,
        losing_trades=losses,
        win_rate=wins / len(trades) if trades else None,
        profit_factor=2.0,
        expectancy_usdt=_D("5"),
        max_drawdown_pct=0.1,
        sharpe_ratio=1.5,
        sortino_ratio=2.1,
        total_gross_pnl=net,
        total_fees_paid=_D("0"),
        total_slippage_cost=_D("0"),
        total_funding_cost=_D("0"),
        total_net_pnl=net,
        final_balance_usdt=_D("100") + net,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_session():
    session = MagicMock()
    session.flush = MagicMock()
    saved: list = []

    def fake_add(obj):
        saved.append(obj)
        if isinstance(obj, BacktestRun) and not obj.id:
            obj.id = "run-uuid-1234"

    session.add = fake_add
    session._saved = saved
    return session


@pytest.fixture()
def candles() -> list[CandleRow]:
    return [_candle(100, offset_hours=i) for i in range(5)]


@pytest.fixture()
def config() -> BacktestConfig:
    return BacktestConfig(symbol="BTCUSDT", timeframe="1h", initial_balance_usdt=_D("100"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunAndPersist:
    def test_persists_all_dod_metrics(self, mock_session, candles, config):
        trades = [_trade(10.0), _trade(-3.0), _trade(7.0)]
        fake_result = _mock_result(trades)

        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.return_value = fake_result
            svc = BacktestService(mock_session)
            run, result = svc.run_and_persist(
                bot_run_id="bot-1",
                config=config,
                candles=candles,
                signal_provider=MagicMock(),
            )

        # BacktestRun
        assert run.status == "COMPLETED"
        assert run.ended_at is not None
        assert run.symbol == "BTCUSDT"

        # BacktestResult — DoD metrics
        assert result.sharpe_ratio == pytest.approx(1.5)
        assert result.sortino_ratio == pytest.approx(2.1)
        assert result.max_drawdown == pytest.approx(0.1)
        assert result.expectancy_usdt == _D("5")
        assert result.win_rate == pytest.approx(2 / 3)
        assert result.profit_factor == pytest.approx(2.0)

        # Counts
        assert result.total_trades == 3
        assert result.winning_trades == 2
        assert result.losing_trades == 1

        # BacktestResult was actually handed to the session
        from backend.storage.models import BacktestResult

        assert any(isinstance(o, BacktestResult) for o in mock_session._saved)

    def test_best_worst_trade_pnl(self, mock_session, candles, config):
        trades = [_trade(10.0), _trade(-3.0), _trade(7.0)]
        fake_result = _mock_result(trades)

        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.return_value = fake_result
            svc = BacktestService(mock_session)
            _, result = svc.run_and_persist(
                bot_run_id="bot-1",
                config=config,
                candles=candles,
                signal_provider=MagicMock(),
            )

        assert result.best_trade_pnl == _D("10")
        assert result.worst_trade_pnl == _D("-3")

    def test_run_fails_marks_status_failed(self, mock_session, candles, config):
        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.side_effect = RuntimeError("engine boom")
            svc = BacktestService(mock_session)

            with pytest.raises(RuntimeError, match="engine boom"):
                svc.run_and_persist(
                    bot_run_id="bot-1",
                    config=config,
                    candles=candles,
                    signal_provider=MagicMock(),
                )

        run = next(o for o in mock_session._saved if isinstance(o, BacktestRun))
        assert run.status == "FAILED"
        assert run.ended_at is not None

    def test_empty_candles_result(self, mock_session, config):
        trades: list[ClosedTrade] = []
        fake_result = _mock_result(trades)
        fake_result = fake_result.model_copy(
            update={
                "win_rate": None,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown_pct": None,
                "expectancy_usdt": None,
                "profit_factor": None,
            }
        )

        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.return_value = fake_result
            svc = BacktestService(mock_session)
            _, result = svc.run_and_persist(
                bot_run_id="bot-1",
                config=config,
                candles=[],
                signal_provider=MagicMock(),
            )

        assert result.total_trades == 0
        assert result.sharpe_ratio is None
        assert result.sortino_ratio is None
        assert result.expectancy_usdt is None

    def test_profit_factor_inf_normalized_to_none(self, mock_session, candles, config):
        # All winning trades → profit_factor = inf → must be stored as None (JSON-safe)
        trades = [_trade(10.0), _trade(5.0)]
        fake_result = _mock_result(trades)
        fake_result = fake_result.model_copy(update={"profit_factor": float("inf")})

        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.return_value = fake_result
            svc = BacktestService(mock_session)
            _, result = svc.run_and_persist(
                bot_run_id="bot-1",
                config=config,
                candles=candles,
                signal_provider=MagicMock(),
            )

        assert result.profit_factor is None

    def test_persist_failed_marks_status_failed(self, mock_session, candles, config):
        trades = [_trade(10.0)]
        fake_result = _mock_result(trades)

        # Force failure via session.flush so the test doesn't depend on import paths.
        # First flush (after save) succeeds; second flush (after COMPLETED) raises.
        mock_session.flush.side_effect = [None, RuntimeError("db error")]

        with patch("backend.backtesting.service.BacktestingEngine") as MockEngine:
            MockEngine.return_value.run.return_value = fake_result
            svc = BacktestService(mock_session)
            with pytest.raises(RuntimeError, match="db error"):
                svc.run_and_persist(
                    bot_run_id="bot-1",
                    config=config,
                    candles=candles,
                    signal_provider=MagicMock(),
                )

        run = next(o for o in mock_session._saved if isinstance(o, BacktestRun))
        assert run.status == "FAILED"
        assert run.ended_at is not None
