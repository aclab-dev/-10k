"""Tests unitarios para ReplayPersistenceService y run_and_persist (F11, [87])."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.replay.metrics import ReplayMetrics
from backend.replay.persistence import ReplayPersistenceService
from backend.replay.schemas import SnapshotWindow
from backend.storage.models import (
    HistoricalReplayRun,
    HistoricalReplaySnapshot,
    StrategyPerformance,
)

_BOT_RUN_ID = str(uuid.uuid4())
_WINDOW = SnapshotWindow(
    symbol="BTCUSDT",
    period_start=datetime(2026, 1, 1, tzinfo=UTC),
    period_end=datetime(2026, 1, 2, tzinfo=UTC),
)
_METRICS_NO_TRADES = ReplayMetrics(
    total_steps=5,
    executed_steps=0,
    signals_activated=2,
    directional_wins=0,
    comparable_executed_steps=0,
    win_rate=None,
    max_drawdown=0.1,
)
_METRICS_WITH_TRADES = ReplayMetrics(
    total_steps=10,
    executed_steps=4,
    signals_activated=6,
    directional_wins=3,
    comparable_executed_steps=4,
    win_rate=0.75,
    max_drawdown=0.2,
)
# Last executed step has no next snapshot: comparable(3) < executed(4)
_METRICS_LAST_UNCLASSIFIED = ReplayMetrics(
    total_steps=10,
    executed_steps=4,
    signals_activated=5,
    directional_wins=2,
    comparable_executed_steps=3,
    win_rate=2 / 3,
    max_drawdown=0.15,
)


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = MagicMock()
    return s


# ---------------------------------------------------------------------------
# open_run
# ---------------------------------------------------------------------------


class TestOpenRun:
    def test_adds_and_flushes_historical_replay_run(self) -> None:
        session = _session()
        svc = ReplayPersistenceService(session)

        run = svc.open_run(_WINDOW, _BOT_RUN_ID)

        session.add.assert_called_once_with(run)
        session.flush.assert_called_once()

    def test_run_has_correct_fields(self) -> None:
        svc = ReplayPersistenceService(_session())

        run = svc.open_run(_WINDOW, _BOT_RUN_ID, config_snapshot={"env": "PAPER"})

        assert isinstance(run, HistoricalReplayRun)
        assert run.bot_run_id == _BOT_RUN_ID
        assert run.symbol == "BTCUSDT"
        assert run.period_start == _WINDOW.period_start
        assert run.period_end == _WINDOW.period_end
        assert run.status == "RUNNING"
        assert run.config_snapshot == {"env": "PAPER"}

    def test_run_id_is_valid_uuid(self) -> None:
        svc = ReplayPersistenceService(_session())
        run = svc.open_run(_WINDOW, _BOT_RUN_ID)
        uuid.UUID(run.id)  # raises if invalid

    def test_empty_config_snapshot_when_none_passed(self) -> None:
        svc = ReplayPersistenceService(_session())
        run = svc.open_run(_WINDOW, _BOT_RUN_ID)
        assert run.config_snapshot == {}


# ---------------------------------------------------------------------------
# save_step
# ---------------------------------------------------------------------------


class TestSaveStep:
    def _make_step_mock(self) -> MagicMock:
        step = MagicMock()
        step.snapshot.model_dump.return_value = {"symbol": "BTCUSDT"}
        step.decision.model_dump.return_value = {"decision": "LONG"}
        step.risk_result.model_dump.return_value = {"decision": "APPROVE"}
        return step

    def test_adds_and_flushes_snapshot(self) -> None:
        session = _session()
        svc = ReplayPersistenceService(session)
        step = self._make_step_mock()

        snap = svc.save_step("replay-run-id", 0, step)

        session.add.assert_called_once_with(snap)
        session.flush.assert_called_once()

    def test_snapshot_has_correct_fields(self) -> None:
        svc = ReplayPersistenceService(_session())
        step = self._make_step_mock()

        snap = svc.save_step("replay-run-id", 3, step)

        assert isinstance(snap, HistoricalReplaySnapshot)
        assert snap.replay_run_id == "replay-run-id"
        assert snap.sequence_num == 3
        assert snap.market_snapshot == {"symbol": "BTCUSDT"}
        assert snap.decision == {"decision": "LONG"}
        assert snap.risk_validation == {"decision": "APPROVE"}
        assert snap.outcome is None

    def test_model_dump_called_with_json_mode(self) -> None:
        svc = ReplayPersistenceService(_session())
        step = self._make_step_mock()
        svc.save_step("replay-run-id", 0, step)
        step.snapshot.model_dump.assert_called_once_with(mode="json")
        step.decision.model_dump.assert_called_once_with(mode="json")
        step.risk_result.model_dump.assert_called_once_with(mode="json")


# ---------------------------------------------------------------------------
# close_run
# ---------------------------------------------------------------------------


class TestCloseRun:
    def _open_run(self, session: MagicMock) -> HistoricalReplayRun:
        svc = ReplayPersistenceService(session)
        return svc.open_run(_WINDOW, _BOT_RUN_ID)

    def test_sets_completed_status_and_ended_at(self) -> None:
        session = _session()
        run = self._open_run(session)
        svc = ReplayPersistenceService(session)

        svc.close_run(run, _METRICS_NO_TRADES, _BOT_RUN_ID)

        assert run.status == "COMPLETED"
        assert run.ended_at is not None
        assert run.total_snapshots == 5

    def test_creates_strategy_performance(self) -> None:
        session = _session()
        run = self._open_run(session)
        svc = ReplayPersistenceService(session)

        perf = svc.close_run(run, _METRICS_WITH_TRADES, _BOT_RUN_ID)

        assert isinstance(perf, StrategyPerformance)
        assert perf.bot_run_id == _BOT_RUN_ID
        assert perf.symbol == "BTCUSDT"
        assert perf.total_trades == 4
        assert perf.winning_trades == 3
        assert perf.losing_trades == 1  # comparable(4) - wins(3) = 1
        assert perf.win_rate == pytest.approx(0.75)
        assert perf.max_drawdown == pytest.approx(0.2)
        assert perf.period_start == _WINDOW.period_start
        assert perf.period_end == _WINDOW.period_end

    def test_win_rate_none_when_no_trades(self) -> None:
        session = _session()
        run = self._open_run(session)
        svc = ReplayPersistenceService(session)

        perf = svc.close_run(run, _METRICS_NO_TRADES, _BOT_RUN_ID)

        assert perf.win_rate is None
        assert perf.total_trades == 0
        assert perf.winning_trades == 0
        assert perf.losing_trades == 0

    def test_losing_trades_uses_comparable_not_executed(self) -> None:
        # executed_steps(4) > comparable_executed_steps(3): last step has no outcome.
        # losing_trades must be comparable(3) - wins(2) = 1, not executed(4) - wins(2) = 2.
        session = _session()
        run = self._open_run(session)
        svc = ReplayPersistenceService(session)

        perf = svc.close_run(run, _METRICS_LAST_UNCLASSIFIED, _BOT_RUN_ID)

        assert perf.total_trades == 4
        assert perf.winning_trades == 2
        assert perf.losing_trades == 1  # comparable(3) - wins(2), not executed(4) - wins(2)


# ---------------------------------------------------------------------------
# fail_run
# ---------------------------------------------------------------------------


class TestFailRun:
    def test_sets_failed_status_and_ended_at(self) -> None:
        session = _session()
        svc = ReplayPersistenceService(session)
        run = svc.open_run(_WINDOW, _BOT_RUN_ID)

        svc.fail_run(run)

        assert run.status == "FAILED"
        assert run.ended_at is not None


# ---------------------------------------------------------------------------
# HistoricalReplayEngine.run_and_persist (integración con mocks)
# ---------------------------------------------------------------------------


class TestRunAndPersist:
    def _make_engine(self) -> tuple[MagicMock, MagicMock]:
        from backend.replay.historical_replay_engine import HistoricalReplayEngine

        session = MagicMock()
        engine = HistoricalReplayEngine(session=session)
        return engine, session

    def test_returns_results_and_metrics(self) -> None:
        from backend.replay.historical_replay_engine import HistoricalReplayEngine

        engine = HistoricalReplayEngine(session=MagicMock())
        fake_results = [MagicMock(), MagicMock()]
        fake_metrics = ReplayMetrics(
            total_steps=2,
            executed_steps=0,
            signals_activated=0,
            directional_wins=0,
            comparable_executed_steps=0,
            win_rate=None,
            max_drawdown=0.0,
        )
        fake_run = MagicMock(id=str(uuid.uuid4()))

        with (
            patch.object(engine, "run", return_value=fake_results),
            patch(
                "backend.replay.persistence.ReplayPersistenceService.open_run",
                return_value=fake_run,
            ),
            patch(
                "backend.replay.persistence.ReplayPersistenceService.save_step"
            ) as mock_save_step,
            patch("backend.replay.persistence.ReplayPersistenceService.close_run"),
            patch("backend.replay.metrics.compute_replay_metrics", return_value=fake_metrics),
        ):
            results, metrics = engine.run_and_persist(
                _WINDOW,
                decision_provider=MagicMock(),
                bot_run_id=_BOT_RUN_ID,
            )

        assert results is fake_results
        assert metrics is fake_metrics
        assert mock_save_step.call_count == len(fake_results)
        for seq, step in enumerate(fake_results):
            mock_save_step.assert_any_call(fake_run.id, seq, step)

    def test_fail_run_called_on_exception(self) -> None:
        from backend.replay.historical_replay_engine import HistoricalReplayEngine

        engine = HistoricalReplayEngine(session=MagicMock())
        fake_run = MagicMock(id=str(uuid.uuid4()))

        with (
            patch.object(engine, "run", side_effect=RuntimeError("boom")),
            patch(
                "backend.replay.persistence.ReplayPersistenceService.open_run",
                return_value=fake_run,
            ),
            patch("backend.replay.persistence.ReplayPersistenceService.fail_run") as mock_fail,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                engine.run_and_persist(
                    _WINDOW,
                    decision_provider=MagicMock(),
                    bot_run_id=_BOT_RUN_ID,
                )

        mock_fail.assert_called_once_with(fake_run)
