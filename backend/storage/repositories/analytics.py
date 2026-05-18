"""Repositorios para el agregado Analytics: performance, replay, backtest, news."""

from __future__ import annotations

from sqlalchemy import select

from backend.storage.models import (
    BacktestResult,
    BacktestRun,
    HistoricalReplayRun,
    HistoricalReplaySnapshot,
    NewsContext,
    StrategyPerformance,
)
from backend.storage.repositories.base import BaseRepository


class StrategyPerformanceRepository(BaseRepository[StrategyPerformance]):
    model = StrategyPerformance

    def list_by_symbol(self, bot_run_id: str, symbol: str) -> list[StrategyPerformance]:
        stmt = select(StrategyPerformance).where(
            StrategyPerformance.bot_run_id == bot_run_id,
            StrategyPerformance.symbol == symbol,
        )
        return list(self._session.scalars(stmt))

    def list_by_regime(self, bot_run_id: str, regime: str) -> list[StrategyPerformance]:
        stmt = select(StrategyPerformance).where(
            StrategyPerformance.bot_run_id == bot_run_id,
            StrategyPerformance.regime == regime,
        )
        return list(self._session.scalars(stmt))


class HistoricalReplayRunRepository(BaseRepository[HistoricalReplayRun]):
    model = HistoricalReplayRun

    def list_by_status(self, bot_run_id: str, status: str) -> list[HistoricalReplayRun]:
        stmt = select(HistoricalReplayRun).where(
            HistoricalReplayRun.bot_run_id == bot_run_id,
            HistoricalReplayRun.status == status,
        )
        return list(self._session.scalars(stmt))


class HistoricalReplaySnapshotRepository(BaseRepository[HistoricalReplaySnapshot]):
    model = HistoricalReplaySnapshot

    def list_by_bot_run(
        self, bot_run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[HistoricalReplaySnapshot]:
        """HistoricalReplaySnapshot no tiene bot_run_id; se une vía replay_run."""
        stmt = (
            select(HistoricalReplaySnapshot)
            .join(
                HistoricalReplayRun,
                HistoricalReplaySnapshot.replay_run_id == HistoricalReplayRun.id,
            )
            .where(HistoricalReplayRun.bot_run_id == bot_run_id)
            .order_by(HistoricalReplaySnapshot.sequence_num)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def list_by_replay_run(
        self, replay_run_id: str, *, limit: int = 500
    ) -> list[HistoricalReplaySnapshot]:
        stmt = (
            select(HistoricalReplaySnapshot)
            .where(HistoricalReplaySnapshot.replay_run_id == replay_run_id)
            .order_by(HistoricalReplaySnapshot.sequence_num)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class BacktestRunRepository(BaseRepository[BacktestRun]):
    model = BacktestRun

    def list_by_status(self, bot_run_id: str, status: str) -> list[BacktestRun]:
        stmt = select(BacktestRun).where(
            BacktestRun.bot_run_id == bot_run_id,
            BacktestRun.status == status,
        )
        return list(self._session.scalars(stmt))


class BacktestResultRepository(BaseRepository[BacktestResult]):
    model = BacktestResult

    def list_by_bot_run(
        self, bot_run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[BacktestResult]:
        """BacktestResult no tiene bot_run_id; se une vía backtest_run."""
        stmt = (
            select(BacktestResult)
            .join(BacktestRun, BacktestResult.backtest_run_id == BacktestRun.id)
            .where(BacktestRun.bot_run_id == bot_run_id)
            .order_by(BacktestResult.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def get_by_backtest_run(self, backtest_run_id: str) -> list[BacktestResult]:
        stmt = select(BacktestResult).where(BacktestResult.backtest_run_id == backtest_run_id)
        return list(self._session.scalars(stmt))


class NewsContextRepository(BaseRepository[NewsContext]):
    model = NewsContext

    def list_by_symbol(self, bot_run_id: str, symbol: str, *, limit: int = 50) -> list[NewsContext]:
        stmt = (
            select(NewsContext)
            .where(
                NewsContext.bot_run_id == bot_run_id,
                NewsContext.symbol == symbol,
            )
            .order_by(NewsContext.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))
