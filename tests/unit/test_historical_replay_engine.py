"""Tests unitarios para HistoricalReplayEngine (F11, tarjetas [85] y [88])."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

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
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.replay.historical_replay_engine import (
    HistoricalReplayEngine,
    snapshot_row_to_pydantic,
)
from backend.replay.schemas import SnapshotWindow
from backend.storage.models import MarketSnapshot as MarketSnapshotRow

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_CANDLE = CandleData(
    open=Decimal("49200"),
    high=Decimal("50000"),
    low=Decimal("49000"),
    close=Decimal("49800"),
    volume=Decimal("500"),
    n_candles=10,
)


def _make_snapshot(symbol: str = "BTCUSDT") -> MarketSnapshot:
    bid = Decimal("50000")
    spread_abs = Decimal("20")
    ask = bid + spread_abs
    return MarketSnapshot(
        timestamp_utc=_NOW,
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
        exchange_server_time=_NOW,
        local_time=_NOW,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _make_gpt_decision(symbol: str = "BTCUSDT") -> ModelDecision:
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=DecisionType.LONG,
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


# ---------------------------------------------------------------------------
# snapshot_row_to_pydantic — round trip con to_db_kwargs
# ---------------------------------------------------------------------------


class TestSnapshotRowToPydantic:
    def test_round_trip_preserves_key_fields(self) -> None:
        original = _make_snapshot()
        kwargs = original.to_db_kwargs(bot_run_id="bot-run-1")
        row = MarketSnapshotRow(**kwargs)
        row.id = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))

        rebuilt = snapshot_row_to_pydantic(row)

        assert rebuilt.snapshot_id == row.id
        assert rebuilt.symbol == original.symbol
        assert rebuilt.last_price == original.last_price
        assert rebuilt.bid == original.bid
        assert rebuilt.ask == original.ask
        assert rebuilt.spread_absolute == original.spread_absolute
        assert rebuilt.candles.tf_5m == original.candles.tf_5m
        assert rebuilt.candles.tf_1h == original.candles.tf_1h
        assert rebuilt.account_balance_usdt == original.account_balance_usdt
        assert rebuilt.data_freshness_status == original.data_freshness_status
        assert rebuilt.coherence_status == original.coherence_status


# ---------------------------------------------------------------------------
# HistoricalReplayEngine.run
# ---------------------------------------------------------------------------


class TestHistoricalReplayEngineRun:
    def test_run_produces_one_result_per_snapshot_in_order(self) -> None:
        snap1 = _make_snapshot()
        snap2 = _make_snapshot()
        row1 = MarketSnapshotRow(**snap1.to_db_kwargs(bot_run_id="bot-run-1"))
        row2 = MarketSnapshotRow(**snap2.to_db_kwargs(bot_run_id="bot-run-1"))

        engine = HistoricalReplayEngine(session=MagicMock())

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        with patch.object(engine._loader, "load", return_value=[row1, row2]):
            results = engine.run(window, decision_provider=lambda snap, _qs: _make_gpt_decision())

        assert len(results) == 2
        assert results[0].snapshot.snapshot_id == snap1.snapshot_id
        assert results[1].snapshot.snapshot_id == snap2.snapshot_id
        for step in results:
            assert step.quant_signals.symbol == "BTCUSDT"
            assert step.regime.primary_regime is not None
            assert step.aggregation.decision_id == step.decision.decision_id
            assert step.risk_result.aggregation_id == step.aggregation.aggregation_id

    def test_run_passes_quant_signals_to_decision_provider(self) -> None:
        snap = _make_snapshot()
        row = MarketSnapshotRow(**snap.to_db_kwargs(bot_run_id="bot-run-1"))

        engine = HistoricalReplayEngine(session=MagicMock())

        received: list[QuantSignalsPackage] = []

        def _provider(_snap: MarketSnapshot, quant_signals: QuantSignalsPackage) -> ModelDecision:
            received.append(quant_signals)
            return _make_gpt_decision()

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        with patch.object(engine._loader, "load", return_value=[row]):
            engine.run(window, decision_provider=_provider)

        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"

    def test_run_respects_daily_loss_for_risk_block(self) -> None:
        snap = _make_snapshot()
        row = MarketSnapshotRow(**snap.to_db_kwargs(bot_run_id="bot-run-1"))

        engine = HistoricalReplayEngine(session=MagicMock())

        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        with patch.object(engine._loader, "load", return_value=[row]):
            results = engine.run(
                window,
                decision_provider=lambda snap, _qs: _make_gpt_decision(),
                daily_loss_usdt=Decimal("15"),
            )

        assert results[0].risk_result.decision.value == "BLOCK"
        assert "daily_drawdown" in results[0].risk_result.reasons

    def test_run_is_idempotent(self) -> None:
        """Mismos snapshots e igual decision_provider → mismas risk decisions y final_actions."""
        snap = _make_snapshot()
        row = MarketSnapshotRow(**snap.to_db_kwargs(bot_run_id="bot-run-1"))

        engine = HistoricalReplayEngine(session=MagicMock())
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        with patch.object(engine._loader, "load", return_value=[row]):
            results_1 = engine.run(window, decision_provider=lambda s, q: _make_gpt_decision())

        with patch.object(engine._loader, "load", return_value=[row]):
            results_2 = engine.run(window, decision_provider=lambda s, q: _make_gpt_decision())

        assert len(results_1) == len(results_2) == 1
        assert results_1[0].risk_result.decision == results_2[0].risk_result.decision
        assert results_1[0].aggregation.final_action == results_2[0].aggregation.final_action
        qs1 = results_1[0].quant_signals
        qs2 = results_2[0].quant_signals
        assert qs1.momentum_signal == qs2.momentum_signal

    def test_run_accumulates_loss_across_steps_and_blocks(self) -> None:
        """Pérdida de step 1 (APPROVE, 10 USDT) se acumula y bloquea step 2 (10% drawdown)."""
        snap1 = _make_snapshot()
        snap2 = _make_snapshot()
        row1 = MarketSnapshotRow(**snap1.to_db_kwargs(bot_run_id="bot-run-acc"))
        row2 = MarketSnapshotRow(**snap2.to_db_kwargs(bot_run_id="bot-run-acc"))

        # Decisión con estimated_max_loss_usdt=10 (= 10% de initial_balance=100 USDT)
        # Step 1 → APPROVE (0% < 10%) → acumula 10 USDT
        # Step 2 → BLOCK (10% >= límite del 10%)
        big_loss_decision = _make_gpt_decision()
        object.__setattr__(big_loss_decision, "estimated_max_loss_usdt", 10.0)
        object.__setattr__(big_loss_decision, "margin_usdt", 10.0)

        engine = HistoricalReplayEngine(session=MagicMock())
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        with patch.object(engine._loader, "load", return_value=[row1, row2]):
            results = engine.run(
                window,
                decision_provider=lambda s, q: big_loss_decision,
                daily_loss_usdt=Decimal("0"),
            )

        assert len(results) == 2
        assert results[0].risk_result.decision.value in ("APPROVE", "ADJUST_DOWN")
        assert results[1].risk_result.decision.value == "BLOCK"
        assert "daily_drawdown" in results[1].risk_result.reasons

    def test_run_empty_window_returns_empty_list(self) -> None:
        engine = HistoricalReplayEngine(session=MagicMock())
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        with patch.object(engine._loader, "load", return_value=[]):
            results = engine.run(window, decision_provider=lambda s, q: _make_gpt_decision())

        assert results == []


# ---------------------------------------------------------------------------
# HistoricalReplayEngine.run_and_persist
# ---------------------------------------------------------------------------


class TestHistoricalReplayEngineRunAndPersist:
    def test_run_and_persist_happy_path(self) -> None:
        """run_and_persist llama open_run, save_step por cada resultado y close_run."""
        snap = _make_snapshot()
        row = MarketSnapshotRow(**snap.to_db_kwargs(bot_run_id="bot-run-p"))

        engine = HistoricalReplayEngine(session=MagicMock())
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        mock_run = MagicMock()
        mock_run.id = "run-uuid-1"
        mock_svc = MagicMock()
        mock_svc.open_run.return_value = mock_run
        mock_metrics = MagicMock()

        with (
            patch.object(engine._loader, "load", return_value=[row]),
            patch(
                "backend.replay.persistence.ReplayPersistenceService",
                return_value=mock_svc,
            ),
            patch(
                "backend.replay.metrics.compute_replay_metrics",
                return_value=mock_metrics,
            ),
        ):
            results, metrics = engine.run_and_persist(
                window,
                decision_provider=lambda s, q: _make_gpt_decision(),
                bot_run_id="bot-run-p",
            )

        mock_svc.open_run.assert_called_once()
        assert mock_svc.save_step.call_count == len(results)
        mock_svc.save_step.assert_has_calls(
            [call(mock_run.id, seq, results[seq]) for seq in range(len(results))],
            any_order=False,
        )
        mock_svc.close_run.assert_called_once()
        mock_svc.fail_run.assert_not_called()
        assert metrics is mock_metrics

    def test_run_and_persist_marks_failed_on_exception(self) -> None:
        """Si run() lanza excepción, se llama fail_run y la excepción se re-lanza."""
        engine = HistoricalReplayEngine(session=MagicMock())
        window = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )

        mock_run = MagicMock()
        mock_svc = MagicMock()
        mock_svc.open_run.return_value = mock_run

        import pytest

        with (
            patch.object(engine._loader, "load", side_effect=RuntimeError("loader boom")),
            patch(
                "backend.replay.persistence.ReplayPersistenceService",
                return_value=mock_svc,
            ),
            patch("backend.replay.metrics.compute_replay_metrics"),
        ):
            with pytest.raises(RuntimeError, match="loader boom"):
                engine.run_and_persist(
                    window,
                    decision_provider=lambda s, q: _make_gpt_decision(),
                    bot_run_id="bot-run-fail",
                )

        mock_svc.fail_run.assert_called_once_with(mock_run)
        mock_svc.close_run.assert_not_called()
