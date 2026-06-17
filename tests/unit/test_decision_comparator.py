"""Tests unitarios para DecisionComparator (F11, tarjeta [86])."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backend.core.config import Environment
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    EntryType,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    ModelDecision,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.schemas import PrimaryRegime
from backend.replay.comparison import DecisionComparator, _build_deltas, _find_nearest
from backend.replay.schemas import ComparisonReport, SnapshotWindow
from backend.storage.models import Decision as DecisionRow
from backend.storage.models import DecisionAggregation as DecisionAggregationRow
from backend.storage.models import RiskValidation as RiskValidationRow

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_CANDLE = CandleData(
    open=Decimal("49200"),
    high=Decimal("50000"),
    low=Decimal("49000"),
    close=Decimal("49800"),
    volume=Decimal("500"),
    n_candles=10,
)


def _make_snapshot(ts: datetime = _NOW, symbol: str = "BTCUSDT") -> MarketSnapshot:
    bid = Decimal("50000")
    spread_abs = Decimal("20")
    ask = bid + spread_abs
    return MarketSnapshot(
        timestamp_utc=ts,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol=symbol,
        last_price=bid + spread_abs / 2,
        bid=bid,
        ask=ask,
        spread_absolute=spread_abs,
        spread_percent=spread_abs / bid * 100,
        candles=Candles(tf_5m=_CANDLE, tf_15m=_CANDLE, tf_1h=_CANDLE, tf_4h=_CANDLE),
        volume=Decimal("50_000_000"),
        funding_rate=0.0001,
        open_interest=Decimal("1_000_000"),
        account_balance_usdt=Decimal("1000"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=ts,
        local_time=ts,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _make_gpt_decision(
    symbol: str = "BTCUSDT",
    decision: DecisionType = DecisionType.LONG,
    ts: datetime = _NOW,
) -> ModelDecision:
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=ts,
        decision=decision,
        symbol=symbol,
        entry_type=EntryType.MARKET,
        entry_price=50_100.0,
        stop_loss=49_500.0,
        take_profit=51_500.0,
        invalidation_price=49_000.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.3,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=15.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_breakout_v1",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=QuantSignalsSection(
            momentum=MomentumInterpretation.BULLISH,
            mean_reversion=MeanReversionInterpretation.NEUTRAL,
            breakout_detection=BreakoutInterpretation.CONFIRMED,
            funding_analysis=FundingInterpretation.NEUTRAL,
            open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
            order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
            liquidity_sweep=LiquiditySweepInterpretation.NONE,
        ),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=0.85,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(
            used=False, impact=NewsImpact.NEUTRAL, summary="No news data used."
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=True,
            partial_close_plan="none",
            max_time_in_trade_minutes=480,
        ),
        decision_rationale_summary="Bullish momentum with strong quant alignment.",
        risk_notes=[],
        execute=True,
    )


def _make_decision_row(ts: datetime = _NOW, action: str = "LONG") -> DecisionRow:
    row = MagicMock(spec=DecisionRow)
    row.timestamp = ts
    row.action = action
    return row


def _make_aggregation_row(
    ts: datetime = _NOW,
    final_action: str = "LONG",
    aggregated_score: float = 0.75,
) -> DecisionAggregationRow:
    row = MagicMock(spec=DecisionAggregationRow)
    row.timestamp = ts
    row.final_action = final_action
    row.aggregated_score = aggregated_score
    return row


def _make_risk_row(ts: datetime = _NOW, result: str = "APPROVE") -> RiskValidationRow:
    row = MagicMock(spec=RiskValidationRow)
    row.timestamp = ts
    row.result = result
    return row


_WINDOW = SnapshotWindow(
    symbol="BTCUSDT",
    period_start=datetime(2026, 1, 1, tzinfo=UTC),
    period_end=datetime(2026, 1, 2, tzinfo=UTC),
)


# ---------------------------------------------------------------------------
# _find_nearest
# ---------------------------------------------------------------------------


class TestFindNearest:
    def test_returns_none_when_list_is_empty(self) -> None:
        assert _find_nearest([], _NOW) is None

    def test_returns_only_record_within_tolerance(self) -> None:
        row = _make_decision_row(ts=_NOW)
        assert _find_nearest([row], _NOW) is row  # type: ignore[arg-type]

    def test_returns_none_when_only_record_exceeds_tolerance(self) -> None:
        row = _make_decision_row(ts=_NOW + timedelta(hours=1))
        assert _find_nearest([row], _NOW) is None  # type: ignore[arg-type]

    def test_returns_nearest_when_multiple_records(self) -> None:
        close = _make_decision_row(ts=_NOW + timedelta(seconds=30))
        far = _make_decision_row(ts=_NOW + timedelta(minutes=3))
        result = _find_nearest([close, far], _NOW)  # type: ignore[arg-type]
        assert result is close

    def test_handles_naive_timestamp_by_assuming_utc(self) -> None:
        naive_ts = _NOW.replace(tzinfo=None)
        row = _make_decision_row(ts=naive_ts)
        # Should match because we treat naive as UTC
        result = _find_nearest([row], _NOW)  # type: ignore[arg-type]
        assert result is row


# ---------------------------------------------------------------------------
# _build_deltas
# ---------------------------------------------------------------------------


class TestBuildDeltas:
    def test_no_changes_when_values_match(self) -> None:
        deltas = _build_deltas("LONG", "LONG", 0.75, "APPROVE", "LONG", "LONG", 0.75, "APPROVE")
        assert all(not d.changed for d in deltas)

    def test_detects_action_change(self) -> None:
        deltas = _build_deltas(
            "LONG", "LONG", 0.75, "APPROVE", "NO_OPERAR", "LONG", 0.75, "APPROVE"
        )
        action_delta = next(d for d in deltas if d.field == "action")
        assert action_delta.changed
        assert action_delta.entonces == "LONG"
        assert action_delta.ahora == "NO_OPERAR"

    def test_detects_risk_result_change(self) -> None:
        deltas = _build_deltas("LONG", "LONG", 0.75, "BLOCK", "LONG", "LONG", 0.75, "APPROVE")
        risk_delta = next(d for d in deltas if d.field == "risk_result")
        assert risk_delta.changed

    def test_float_comparison_ignores_tiny_differences(self) -> None:
        # Diferencia menor a 1e-6: no debe considerarse cambio
        deltas = _build_deltas(
            "LONG", "LONG", 0.750000001, "APPROVE", "LONG", "LONG", 0.75, "APPROVE"
        )
        score_delta = next(d for d in deltas if d.field == "aggregated_score")
        assert not score_delta.changed

    def test_none_historical_aggregated_score_is_not_changed(self) -> None:
        deltas = _build_deltas("LONG", "LONG", None, "APPROVE", "LONG", "LONG", 0.75, "APPROVE")
        score_delta = next(d for d in deltas if d.field == "aggregated_score")
        # None entonces vs valor ahora: changed=True (string comparison: None != "0.750000")
        assert score_delta.changed

    def test_returns_four_deltas(self) -> None:
        deltas = _build_deltas("LONG", "LONG", 0.75, "APPROVE", "LONG", "LONG", 0.75, "APPROVE")
        assert len(deltas) == 4
        fields = {d.field for d in deltas}
        assert fields == {"action", "final_action", "aggregated_score", "risk_result"}


# ---------------------------------------------------------------------------
# DecisionComparator.compare — integración ligera con mocks
# ---------------------------------------------------------------------------


class TestDecisionComparatorCompare:
    def _build_comparator(self) -> DecisionComparator:
        return DecisionComparator(session=MagicMock())

    def _make_replay_step(
        self,
        decision_type: DecisionType = DecisionType.LONG,
        final_action_value: str = "LONG",
        aggregated_score: float = 0.75,
        risk_decision_value: str = "APPROVE",
        snapshot_ts: datetime = _NOW,
    ) -> MagicMock:
        step = MagicMock()
        step.snapshot = _make_snapshot(ts=snapshot_ts)
        step.decision = _make_gpt_decision(decision=decision_type, ts=snapshot_ts)
        step.aggregation.final_action.value = final_action_value
        step.aggregation.aggregated_score = aggregated_score
        step.aggregation.decision_id = step.decision.decision_id
        step.risk_result.decision.value = risk_decision_value
        return step

    def test_returns_report_with_correct_total_steps(self) -> None:
        comparator = self._build_comparator()
        replay_step = self._make_replay_step()
        with (
            patch.object(comparator._engine, "run", return_value=[replay_step]),
            patch.object(comparator._decision_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._aggregation_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._risk_repo, "list_by_symbol_and_range", return_value=[]),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        assert isinstance(report, ComparisonReport)
        assert report.total_steps == 1

    def test_marks_missing_when_no_historical_records(self) -> None:
        comparator = self._build_comparator()
        replay_step = self._make_replay_step()

        with (
            patch.object(comparator._engine, "run", return_value=[replay_step]),
            patch.object(comparator._decision_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._aggregation_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._risk_repo, "list_by_symbol_and_range", return_value=[]),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        assert report.missing_steps == 1
        assert report.changed_steps == 0
        assert report.steps[0].historical_missing is True

    def test_detects_no_change_when_values_match(self) -> None:
        comparator = self._build_comparator()
        replay_step = self._make_replay_step(
            decision_type=DecisionType.LONG,
            final_action_value="LONG",
            aggregated_score=0.75,
            risk_decision_value="APPROVE",
        )
        hist_decision = _make_decision_row(action="LONG")
        hist_aggregation = _make_aggregation_row(final_action="LONG", aggregated_score=0.75)
        hist_risk = _make_risk_row(result="APPROVE")

        with (
            patch.object(comparator._engine, "run", return_value=[replay_step]),
            patch.object(
                comparator._decision_repo,
                "list_by_symbol_and_range",
                return_value=[hist_decision],
            ),
            patch.object(
                comparator._aggregation_repo,
                "list_by_symbol_and_range",
                return_value=[hist_aggregation],
            ),
            patch.object(
                comparator._risk_repo, "list_by_symbol_and_range", return_value=[hist_risk]
            ),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        assert report.changed_steps == 0
        assert report.change_rate == 0.0
        assert not report.steps[0].any_changed

    def test_detects_change_when_risk_result_differs(self) -> None:
        comparator = self._build_comparator()
        replay_step = self._make_replay_step(risk_decision_value="APPROVE")
        hist_decision = _make_decision_row(action="LONG")
        hist_aggregation = _make_aggregation_row(final_action="LONG", aggregated_score=0.75)
        hist_risk = _make_risk_row(result="BLOCK")  # entonces=BLOCK, ahora=APPROVE

        with (
            patch.object(comparator._engine, "run", return_value=[replay_step]),
            patch.object(
                comparator._decision_repo,
                "list_by_symbol_and_range",
                return_value=[hist_decision],
            ),
            patch.object(
                comparator._aggregation_repo,
                "list_by_symbol_and_range",
                return_value=[hist_aggregation],
            ),
            patch.object(
                comparator._risk_repo, "list_by_symbol_and_range", return_value=[hist_risk]
            ),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        assert report.changed_steps == 1
        assert report.change_rate == 1.0
        step = report.steps[0]
        assert step.any_changed
        risk_delta = next(d for d in step.deltas if d.field == "risk_result")
        assert risk_delta.changed
        assert risk_delta.entonces == "BLOCK"
        assert risk_delta.ahora == "APPROVE"

    def test_change_rate_excludes_missing_steps(self) -> None:
        comparator = self._build_comparator()
        ts1 = _NOW
        ts2 = _NOW + timedelta(minutes=30)
        step1 = self._make_replay_step(snapshot_ts=ts1, risk_decision_value="BLOCK")
        step2 = self._make_replay_step(snapshot_ts=ts2, risk_decision_value="APPROVE")

        hist_decision1 = _make_decision_row(ts=ts1, action="LONG")
        hist_agg1 = _make_aggregation_row(ts=ts1, final_action="LONG", aggregated_score=0.75)
        # entonces=APPROVE, ahora=BLOCK → cambio
        hist_risk1 = _make_risk_row(ts=ts1, result="APPROVE")

        # step2 no tiene registros históricos → missing
        with (
            patch.object(comparator._engine, "run", return_value=[step1, step2]),
            patch.object(
                comparator._decision_repo,
                "list_by_symbol_and_range",
                return_value=[hist_decision1],
            ),
            patch.object(
                comparator._aggregation_repo,
                "list_by_symbol_and_range",
                return_value=[hist_agg1],
            ),
            patch.object(
                comparator._risk_repo, "list_by_symbol_and_range", return_value=[hist_risk1]
            ),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        # 2 steps total: 1 changed, 1 missing → change_rate = 1/1 = 1.0
        assert report.total_steps == 2
        assert report.changed_steps == 1
        assert report.missing_steps == 1
        assert report.change_rate == pytest.approx(1.0)

    def test_empty_window_returns_zero_rate(self) -> None:
        comparator = self._build_comparator()

        with (
            patch.object(comparator._engine, "run", return_value=[]),
            patch.object(comparator._decision_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._aggregation_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._risk_repo, "list_by_symbol_and_range", return_value=[]),
        ):
            report = comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision())

        assert report.total_steps == 0
        assert report.change_rate == 0.0

    def test_report_exposes_bot_run_id(self) -> None:
        comparator = self._build_comparator()

        with (
            patch.object(comparator._engine, "run", return_value=[]),
            patch.object(comparator._decision_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._aggregation_repo, "list_by_symbol_and_range", return_value=[]),
            patch.object(comparator._risk_repo, "list_by_symbol_and_range", return_value=[]),
        ):
            report = comparator.compare(
                _WINDOW, lambda s, q: _make_gpt_decision(), bot_run_id="run-abc"
            )

        assert report.bot_run_id == "run-abc"

    def test_passes_bot_run_id_to_repos(self) -> None:
        comparator = self._build_comparator()

        with (
            patch.object(comparator._engine, "run", return_value=[]),
            patch.object(
                comparator._decision_repo, "list_by_symbol_and_range", return_value=[]
            ) as mock_dec,
            patch.object(
                comparator._aggregation_repo, "list_by_symbol_and_range", return_value=[]
            ) as mock_agg,
            patch.object(
                comparator._risk_repo, "list_by_symbol_and_range", return_value=[]
            ) as mock_risk,
        ):
            comparator.compare(_WINDOW, lambda s, q: _make_gpt_decision(), bot_run_id="run-42")

        mock_dec.assert_called_once_with(
            symbol="BTCUSDT",
            period_start=_WINDOW.period_start,
            period_end=_WINDOW.period_end,
            bot_run_id="run-42",
        )
        mock_agg.assert_called_once_with(
            symbol="BTCUSDT",
            period_start=_WINDOW.period_start,
            period_end=_WINDOW.period_end,
            bot_run_id="run-42",
        )
        mock_risk.assert_called_once_with(
            symbol="BTCUSDT",
            period_start=_WINDOW.period_start,
            period_end=_WINDOW.period_end,
            bot_run_id="run-42",
        )
