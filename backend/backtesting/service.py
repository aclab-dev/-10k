"""Servicio de orquestación del Backtesting Engine (F12).

Responsabilidades:
- Crear el registro BacktestRun en la DB.
- Ejecutar BacktestingEngine.run() con el SignalProvider provisto.
- Persistir BacktestResult con todas las métricas calculadas.
- Actualizar BacktestRun.status y ended_at al finalizar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.orm import Session

from backend.backtesting.engine import BacktestingEngine, SignalProvider
from backend.backtesting.fee_model import FeeModel
from backend.backtesting.latency_model import LatencyModel
from backend.backtesting.schemas import BacktestConfig, BacktestRunResult, CandleRow
from backend.backtesting.slippage_model import SlippageModel
from backend.storage.models import BacktestResult, BacktestRun
from backend.storage.repositories.analytics import BacktestResultRepository, BacktestRunRepository

_log = structlog.get_logger(__name__)


class BacktestService:
    """Orquesta la ejecución y persistencia de un backtest run completo."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._run_repo = BacktestRunRepository(session)
        self._result_repo = BacktestResultRepository(session)

    def run_and_persist(
        self,
        *,
        bot_run_id: str,
        config: BacktestConfig,
        candles: list[CandleRow],
        signal_provider: SignalProvider,
        fee_model: FeeModel | None = None,
        slippage_model: SlippageModel | None = None,
        latency_model: LatencyModel | None = None,
    ) -> tuple[BacktestRun, BacktestResult]:
        """Ejecuta el backtest y persiste los resultados.

        Returns:
            Tupla (BacktestRun, BacktestResult) con los registros persistidos.

        Raises:
            Exception: cualquier error del engine. BacktestRun queda con status FAILED.
        """
        period_start = candles[0].timestamp_utc if candles else datetime.now(timezone.utc)
        period_end = candles[-1].timestamp_utc if candles else datetime.now(timezone.utc)

        engine = BacktestingEngine(
            config=config,
            fee_model=fee_model,
            slippage_model=slippage_model,
            latency_model=latency_model,
        )

        run = BacktestRun(
            bot_run_id=bot_run_id,
            symbol=config.symbol,
            period_start=period_start,
            period_end=period_end,
            config_snapshot=config.model_dump(mode="json"),
            fee_model=str(fee_model) if fee_model else None,
            slippage_model=str(slippage_model) if slippage_model else None,
            status="RUNNING",
        )
        self._run_repo.save(run)
        _log.info("backtest_run_started", run_id=run.id, symbol=config.symbol)

        try:
            result_data: BacktestRunResult = engine.run(candles, signal_provider)
        except Exception:
            run.status = "FAILED"
            run.ended_at = datetime.now(timezone.utc)
            self._session.flush()
            _log.exception("backtest_run_failed", run_id=run.id)
            raise

        result = self._persist_result(run.id, result_data)

        run.status = "COMPLETED"
        run.ended_at = datetime.now(timezone.utc)
        self._session.flush()

        _log.info(
            "backtest_run_completed",
            run_id=run.id,
            total_trades=result_data.total_trades,
            sharpe=result_data.sharpe_ratio,
            sortino=result_data.sortino_ratio,
            max_drawdown=result_data.max_drawdown_pct,
            expectancy_usdt=str(result_data.expectancy_usdt),
            hit_ratio=result_data.win_rate,
        )
        return run, result

    def _persist_result(self, backtest_run_id: str, r: BacktestRunResult) -> BacktestResult:
        trades = r.trades
        best_pnl = max((t.net_pnl_usdt for t in trades), default=Decimal("0"))
        worst_pnl = min((t.net_pnl_usdt for t in trades), default=Decimal("0"))

        result = BacktestResult(
            backtest_run_id=backtest_run_id,
            total_trades=r.total_trades,
            winning_trades=r.winning_trades,
            losing_trades=r.losing_trades,
            win_rate=r.win_rate,
            total_pnl=r.total_net_pnl,
            gross_pnl=r.total_gross_pnl,
            total_fees=r.total_fees_paid,
            total_funding=r.total_funding_cost,
            sharpe_ratio=r.sharpe_ratio,
            sortino_ratio=r.sortino_ratio,
            max_drawdown=r.max_drawdown_pct,
            expectancy_usdt=r.expectancy_usdt,
            profit_factor=r.profit_factor,
            best_trade_pnl=best_pnl,
            worst_trade_pnl=worst_pnl,
        )
        return self._result_repo.save(result)
