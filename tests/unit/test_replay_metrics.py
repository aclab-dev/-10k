"""Tests unitarios para compute_replay_metrics (F11, [87])."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.decision_engine.schemas import DecisionType
from backend.replay.metrics import ReplayMetrics, _compute_max_drawdown, compute_replay_metrics
from backend.risk_engine.schemas import RiskDecision


def _make_step(
    *,
    quant_score: float = 0.6,
    aggregated_score: float = 0.65,
    execute: bool = True,
    decision_type: DecisionType = DecisionType.LONG,
    entry_price: float = 50_000.0,
    risk_decision: RiskDecision = RiskDecision.APPROVE,
    last_price: Decimal = Decimal("50100"),
) -> MagicMock:
    step = MagicMock()
    step.aggregation.contributing_sources.quant_score = quant_score
    step.aggregation.aggregated_score = aggregated_score
    step.decision.execute = execute
    step.decision.decision = decision_type
    step.decision.entry_price = entry_price
    step.risk_result.decision = risk_decision
    step.snapshot.last_price = last_price
    return step


# ---------------------------------------------------------------------------
# Casos base
# ---------------------------------------------------------------------------


class TestComputeReplayMetricsEmpty:
    def test_empty_returns_zero_metrics(self) -> None:
        m = compute_replay_metrics([])
        assert m == ReplayMetrics(
            total_steps=0,
            executed_steps=0,
            signals_activated=0,
            directional_wins=0,
            comparable_executed_steps=0,
            win_rate=None,
            max_drawdown=0.0,
        )


class TestComputeReplayMetricsSingleStep:
    def test_single_step_no_comparison_possible(self) -> None:
        step = _make_step()
        m = compute_replay_metrics([step])
        assert m.total_steps == 1
        assert m.executed_steps == 1
        assert m.comparable_executed_steps == 0
        assert m.win_rate is None

    def test_single_no_operar_step_not_executed(self) -> None:
        step = _make_step(decision_type=DecisionType.NO_OPERAR, execute=False)
        m = compute_replay_metrics([step])
        assert m.executed_steps == 0

    def test_single_blocked_step_not_executed(self) -> None:
        step = _make_step(risk_decision=RiskDecision.BLOCK)
        m = compute_replay_metrics([step])
        assert m.executed_steps == 0


# ---------------------------------------------------------------------------
# signals_activated
# ---------------------------------------------------------------------------


class TestSignalsActivated:
    def test_counts_steps_above_threshold(self) -> None:
        steps = [
            _make_step(quant_score=0.3),   # below threshold
            _make_step(quant_score=0.5),   # at threshold — not above
            _make_step(quant_score=0.51),  # above
            _make_step(quant_score=0.9),   # above
        ]
        m = compute_replay_metrics(steps)
        assert m.signals_activated == 2

    def test_no_signals_when_all_below(self) -> None:
        steps = [_make_step(quant_score=0.1), _make_step(quant_score=0.4)]
        m = compute_replay_metrics(steps)
        assert m.signals_activated == 0


# ---------------------------------------------------------------------------
# executed_steps y win_rate
# ---------------------------------------------------------------------------


class TestExecutedSteps:
    def test_approve_and_adjust_down_count_as_executed(self) -> None:
        steps = [
            _make_step(risk_decision=RiskDecision.APPROVE),
            _make_step(risk_decision=RiskDecision.ADJUST_DOWN),
            _make_step(risk_decision=RiskDecision.BLOCK),
            _make_step(risk_decision=RiskDecision.NO_OPERAR, execute=False,
                       decision_type=DecisionType.NO_OPERAR),
        ]
        m = compute_replay_metrics(steps)
        assert m.executed_steps == 2

    def test_execute_false_not_counted(self) -> None:
        step = _make_step(execute=False, risk_decision=RiskDecision.APPROVE)
        m = compute_replay_metrics([step])
        assert m.executed_steps == 0


class TestWinRate:
    def test_long_directional_win_when_next_price_higher(self) -> None:
        steps = [
            _make_step(decision_type=DecisionType.LONG, entry_price=50_000.0,
                       last_price=Decimal("50000")),
            _make_step(last_price=Decimal("50100")),  # next price > entry
        ]
        m = compute_replay_metrics(steps)
        assert m.directional_wins == 1
        assert m.win_rate == pytest.approx(1.0)

    def test_long_directional_loss_when_next_price_lower(self) -> None:
        steps = [
            _make_step(decision_type=DecisionType.LONG, entry_price=50_000.0),
            _make_step(last_price=Decimal("49000")),  # next price < entry
        ]
        m = compute_replay_metrics(steps)
        assert m.directional_wins == 0
        assert m.win_rate == pytest.approx(0.0)

    def test_short_directional_win_when_next_price_lower(self) -> None:
        steps = [
            _make_step(decision_type=DecisionType.SHORT, entry_price=50_000.0),
            _make_step(last_price=Decimal("49000")),
        ]
        m = compute_replay_metrics(steps)
        assert m.directional_wins == 1
        assert m.win_rate == pytest.approx(1.0)

    def test_short_directional_loss_when_next_price_higher(self) -> None:
        steps = [
            _make_step(decision_type=DecisionType.SHORT, entry_price=50_000.0),
            _make_step(last_price=Decimal("51000")),
        ]
        m = compute_replay_metrics(steps)
        assert m.directional_wins == 0
        assert m.win_rate == pytest.approx(0.0)

    def test_win_rate_across_multiple_steps(self) -> None:
        # 3 executed steps: steps 0, 1, 2 (step 3 is not executed)
        # step 0: LONG entry=50000, next=step1.last=51000 → win
        # step 1: LONG entry=50000, next=step2.last=49000 → loss
        # step 2: LONG entry=50000, next=step3.last=51000 → win (but step2 is last executable)
        # step 3: not executed (BLOCK)
        steps = [
            _make_step(decision_type=DecisionType.LONG, entry_price=50_000.0,
                       last_price=Decimal("50000")),
            _make_step(decision_type=DecisionType.LONG, entry_price=50_000.0,
                       last_price=Decimal("51000")),  # next for step 0
            _make_step(decision_type=DecisionType.LONG, entry_price=50_000.0,
                       last_price=Decimal("49000")),  # next for step 1
            _make_step(risk_decision=RiskDecision.BLOCK,
                       last_price=Decimal("51000")),  # next for step 2, but step 2 win/loss needs step 3
        ]
        m = compute_replay_metrics(steps)
        # step 0: executed, comparable, next=step1 last=51000 > 50000 → win
        # step 1: executed, comparable, next=step2 last=49000 < 50000 → loss
        # step 2: executed, comparable, next=step3 last=51000 > 50000 → win
        # step 3: BLOCK → not executed
        assert m.executed_steps == 3
        assert m.comparable_executed_steps == 3
        assert m.directional_wins == 2
        assert m.win_rate == pytest.approx(2 / 3)

    def test_none_win_rate_when_no_comparable_executed_steps(self) -> None:
        # Only one executed step (the last), no next snapshot
        step = _make_step()
        m = compute_replay_metrics([step])
        assert m.win_rate is None


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------


class TestComputeMaxDrawdown:
    def test_no_drawdown_monotonically_rising(self) -> None:
        assert _compute_max_drawdown([0.3, 0.5, 0.7, 0.9]) == pytest.approx(0.0)

    def test_single_element_returns_zero(self) -> None:
        assert _compute_max_drawdown([0.5]) == pytest.approx(0.0)

    def test_empty_returns_zero(self) -> None:
        assert _compute_max_drawdown([]) == pytest.approx(0.0)

    def test_simple_drawdown(self) -> None:
        # Peak 1.0, trough 0.5 → 50% drawdown
        dd = _compute_max_drawdown([0.5, 1.0, 0.5])
        assert dd == pytest.approx(0.5)

    def test_max_of_multiple_drawdowns(self) -> None:
        # Peak=1.0, global trough=0.5 → max drawdown = (1.0-0.5)/1.0 = 0.5
        dd = _compute_max_drawdown([0.5, 1.0, 0.8, 0.9, 0.5])
        assert dd == pytest.approx(0.5)

    def test_monotonically_falling(self) -> None:
        # Peak=first element, max drawdown from 0.9 to 0.1
        dd = _compute_max_drawdown([0.9, 0.7, 0.5, 0.1])
        assert dd == pytest.approx((0.9 - 0.1) / 0.9, rel=1e-6)


class TestComputeReplayMetricsMaxDrawdown:
    def test_max_drawdown_propagated_from_aggregated_scores(self) -> None:
        steps = [
            _make_step(aggregated_score=0.5),
            _make_step(aggregated_score=1.0),
            _make_step(aggregated_score=0.5),
        ]
        m = compute_replay_metrics(steps)
        assert m.max_drawdown == pytest.approx(0.5)
