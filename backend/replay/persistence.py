"""Persistencia de replay runs y métricas en DB (F11, [87]).

ReplayPersistenceService encapsula la escritura de:
- historical_replay_runs: ciclo de vida RUNNING → COMPLETED / FAILED
- historical_replay_snapshots: un registro por cada ReplayStepResult
- strategy_performance: métricas agregadas (win_rate, drawdown, señales)

Convención de transaccionalidad: flush() pero no commit().
El caller gestiona la transacción y decide cuándo commitear o hacer rollback.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.replay.historical_replay_engine import ReplayStepResult
from backend.replay.metrics import ReplayMetrics
from backend.replay.schemas import SnapshotWindow
from backend.storage.models import (
    HistoricalReplayRun,
    HistoricalReplaySnapshot,
    StrategyPerformance,
)


class ReplayPersistenceService:
    """Encapsula la escritura de replay runs, snapshots y métricas en DB."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def open_run(
        self,
        window: SnapshotWindow,
        bot_run_id: str,
        config_snapshot: dict[str, Any] | None = None,
    ) -> HistoricalReplayRun:
        """Crea y flushea un HistoricalReplayRun en estado RUNNING."""
        run = HistoricalReplayRun(
            id=str(uuid.uuid4()),
            bot_run_id=bot_run_id,
            symbol=window.symbol,
            period_start=window.period_start,
            period_end=window.period_end,
            config_snapshot=config_snapshot or {},
            status="RUNNING",
        )
        self._session.add(run)
        self._session.flush()
        return run

    def save_step(
        self,
        replay_run_id: str,
        sequence_num: int,
        step: ReplayStepResult,
    ) -> HistoricalReplaySnapshot:
        """Persiste un paso del replay como HistoricalReplaySnapshot."""
        record = HistoricalReplaySnapshot(
            id=str(uuid.uuid4()),
            replay_run_id=replay_run_id,
            sequence_num=sequence_num,
            market_snapshot=step.snapshot.model_dump(mode="json"),
            decision=step.decision.model_dump(mode="json"),
            risk_validation=step.risk_result.model_dump(mode="json"),
            outcome=None,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def close_run(
        self,
        run: HistoricalReplayRun,
        metrics: ReplayMetrics,
        bot_run_id: str,
    ) -> StrategyPerformance:
        """Cierra el run (COMPLETED) y persiste las métricas en strategy_performance.

        winning_trades y losing_trades se calculan sobre comparable_executed_steps
        (pasos con snapshot siguiente disponible). El último paso ejecutado, si
        no tiene siguiente snapshot, queda sin clasificar; por eso
        winning + losing puede ser menor que total_trades.
        """
        run.status = "COMPLETED"
        run.ended_at = datetime.now(UTC)
        run.total_snapshots = metrics.total_steps
        self._session.flush()

        losing_trades = metrics.comparable_executed_steps - metrics.directional_wins
        perf = StrategyPerformance(
            id=str(uuid.uuid4()),
            bot_run_id=bot_run_id,
            symbol=run.symbol,
            total_trades=metrics.executed_steps,
            winning_trades=metrics.directional_wins,
            losing_trades=losing_trades,
            win_rate=metrics.win_rate,
            max_drawdown=metrics.max_drawdown,
            period_start=run.period_start,
            period_end=run.period_end,
        )
        self._session.add(perf)
        self._session.flush()
        return perf

    def fail_run(self, run: HistoricalReplayRun) -> None:
        """Marca el run como FAILED."""
        run.status = "FAILED"
        run.ended_at = datetime.now(UTC)
        self._session.flush()
