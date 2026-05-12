"""create_all_annexb_tables

Revision ID: c1edf83a521c
Revises:
Create Date: 2026-05-12

Crea las 27 tablas del Anexo B (PDF §8.2).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "c1edf83a521c"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. bot_runs
    op.create_table(
        "bot_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=False),
        sa.Column("config_snapshot", JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="RUNNING"),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # 2. bot_state
    op.create_table(
        "bot_state",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_bot_state_bot_run_id", "bot_state", ["bot_run_id"])

    # 3. accounts_state
    op.create_table(
        "accounts_state",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("balance_usdt", sa.Float(), nullable=False),
        sa.Column("equity_usdt", sa.Float(), nullable=False),
        sa.Column("margin_used_usdt", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("realized_pnl_session", sa.Float(), nullable=False, server_default="0"),
        sa.Column("drawdown_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("exposure_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("environment", sa.String(16), nullable=False),
    )
    op.create_index("ix_accounts_state_bot_run_id", "accounts_state", ["bot_run_id"])
    op.create_index("ix_accounts_state_timestamp", "accounts_state", ["timestamp"])

    # 4. market_snapshots
    op.create_table(
        "market_snapshots",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("funding_rate", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("spread", sa.Float(), nullable=True),
        sa.Column("extra", JSONB(), nullable=True),
    )
    op.create_index("ix_market_snapshots_bot_run_id", "market_snapshots", ["bot_run_id"])
    op.create_index("ix_market_snapshots_symbol_timestamp", "market_snapshots", ["symbol", "timestamp"])

    # 5. quant_signals
    op.create_table(
        "quant_signals",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market_snapshot_id", UUID(as_uuid=False), sa.ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("momentum_score", sa.Float(), nullable=True),
        sa.Column("mean_reversion_score", sa.Float(), nullable=True),
        sa.Column("breakout_score", sa.Float(), nullable=True),
        sa.Column("liquidity_sweep_score", sa.Float(), nullable=True),
        sa.Column("order_flow_score", sa.Float(), nullable=True),
        sa.Column("funding_signal", sa.Float(), nullable=True),
        sa.Column("open_interest_signal", sa.Float(), nullable=True),
        sa.Column("composite_score", sa.Float(), nullable=True),
        sa.Column("raw_signals", JSONB(), nullable=True),
    )
    op.create_index("ix_quant_signals_bot_run_id", "quant_signals", ["bot_run_id"])
    op.create_index("ix_quant_signals_symbol_timestamp", "quant_signals", ["symbol", "timestamp"])

    # 6. market_regimes
    op.create_table(
        "market_regimes",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market_snapshot_id", UUID(as_uuid=False), sa.ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("regime_details", JSONB(), nullable=True),
    )
    op.create_index("ix_market_regimes_bot_run_id", "market_regimes", ["bot_run_id"])
    op.create_index("ix_market_regimes_symbol_timestamp", "market_regimes", ["symbol", "timestamp"])

    # 7. volatility_assessments
    op.create_table(
        "volatility_assessments",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market_snapshot_id", UUID(as_uuid=False), sa.ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("atr_percent", sa.Float(), nullable=True),
        sa.Column("volatility_score", sa.Float(), nullable=True),
        sa.Column("leverage_cap", sa.Integer(), nullable=True),
        sa.Column("liquidation_risk_score", sa.Float(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
    )
    op.create_index("ix_volatility_assessments_bot_run_id", "volatility_assessments", ["bot_run_id"])

    # 8. feature_packages
    op.create_table(
        "feature_packages",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market_snapshot_id", UUID(as_uuid=False), sa.ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("features", JSONB(), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_feature_packages_bot_run_id", "feature_packages", ["bot_run_id"])
    op.create_index("ix_feature_packages_hash", "feature_packages", ["hash"])

    # 9. model_requests
    op.create_table(
        "model_requests",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("feature_package_id", UUID(as_uuid=False), sa.ForeignKey("feature_packages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_tokens_estimate", sa.Integer(), nullable=True),
        sa.Column("context", JSONB(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_model_requests_bot_run_id", "model_requests", ["bot_run_id"])

    # 10. model_responses
    op.create_table(
        "model_responses",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("model_request_id", UUID(as_uuid=False), sa.ForeignKey("model_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_response", sa.Text(), nullable=False),
        sa.Column("normalized_response", JSONB(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("finish_reason", sa.String(32), nullable=True),
        sa.Column("is_valid_schema", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_model_responses_model_request_id", "model_responses", ["model_request_id"])

    # 11. decisions
    op.create_table(
        "decisions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_response_id", UUID(as_uuid=False), sa.ForeignKey("model_responses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("margin_usdt", sa.Float(), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=True),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("raw_decision", JSONB(), nullable=True),
    )
    op.create_index("ix_decisions_bot_run_id", "decisions", ["bot_run_id"])
    op.create_index("ix_decisions_symbol_timestamp", "decisions", ["symbol", "timestamp"])

    # 12. decision_aggregations
    op.create_table(
        "decision_aggregations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_id", UUID(as_uuid=False), sa.ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("quant_score", sa.Float(), nullable=True),
        sa.Column("gpt_confidence", sa.Float(), nullable=True),
        sa.Column("regime_factor", sa.Float(), nullable=True),
        sa.Column("volatility_factor", sa.Float(), nullable=True),
        sa.Column("aggregated_score", sa.Float(), nullable=True),
        sa.Column("final_action", sa.String(16), nullable=False),
        sa.Column("reasons", JSONB(), nullable=True),
    )
    op.create_index("ix_decision_aggregations_bot_run_id", "decision_aggregations", ["bot_run_id"])

    # 13. risk_validations
    op.create_table(
        "risk_validations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_aggregation_id", UUID(as_uuid=False), sa.ForeignKey("decision_aggregations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("original_margin", sa.Float(), nullable=True),
        sa.Column("original_leverage", sa.Integer(), nullable=True),
        sa.Column("adjusted_margin", sa.Float(), nullable=True),
        sa.Column("adjusted_leverage", sa.Integer(), nullable=True),
        sa.Column("reasons", JSONB(), nullable=True),
        sa.Column("daily_loss_at_check", sa.Float(), nullable=True),
        sa.Column("total_loss_at_check", sa.Float(), nullable=True),
    )
    op.create_index("ix_risk_validations_bot_run_id", "risk_validations", ["bot_run_id"])
    op.create_index("ix_risk_validations_result", "risk_validations", ["result"])

    # 14. trades
    op.create_table(
        "trades",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("risk_validation_id", UUID(as_uuid=False), sa.ForeignKey("risk_validations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("margin_usdt", sa.Float(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("funding_paid", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("close_reason", sa.String(32), nullable=True),
    )
    op.create_index("ix_trades_bot_run_id", "trades", ["bot_run_id"])
    op.create_index("ix_trades_symbol_status", "trades", ["symbol", "status"])

    # 15. orders
    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_id", UUID(as_uuid=False), sa.ForeignKey("trades.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("exchange_order_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("filled_at", sa.DateTime(), nullable=True),
        sa.Column("fill_price", sa.Float(), nullable=True),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("is_simulated", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_orders_bot_run_id", "orders", ["bot_run_id"])
    op.create_index("ix_orders_trade_id", "orders", ["trade_id"])

    # 16. positions
    op.create_table(
        "positions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_id", UUID(as_uuid=False), sa.ForeignKey("trades.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("margin_usdt", sa.Float(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("trailing_stop_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("break_even_triggered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_positions_bot_run_id", "positions", ["bot_run_id"])
    op.create_index("ix_positions_symbol_status", "positions", ["symbol", "status"])

    # 17. position_events
    op.create_table(
        "position_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("position_id", UUID(as_uuid=False), sa.ForeignKey("positions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("old_value", sa.Float(), nullable=True),
        sa.Column("new_value", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_position_events_position_id", "position_events", ["position_id"])

    # 18. strategy_performance
    op.create_table(
        "strategy_performance",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("regime", sa.String(32), nullable=True),
        sa.Column("setup_type", sa.String(64), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losing_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("avg_pnl", sa.Float(), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=True),
        sa.Column("sharpe_ratio", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_strategy_performance_bot_run_id", "strategy_performance", ["bot_run_id"])
    op.create_index("ix_strategy_performance_symbol", "strategy_performance", ["symbol"])

    # 19. historical_replay_runs
    op.create_table(
        "historical_replay_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("config_snapshot", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
        sa.Column("total_snapshots", sa.Integer(), nullable=True),
    )
    op.create_index("ix_historical_replay_runs_bot_run_id", "historical_replay_runs", ["bot_run_id"])

    # 20. historical_replay_snapshots
    op.create_table(
        "historical_replay_snapshots",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("replay_run_id", UUID(as_uuid=False), sa.ForeignKey("historical_replay_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_num", sa.BigInteger(), nullable=False),
        sa.Column("market_snapshot", JSONB(), nullable=False),
        sa.Column("decision", JSONB(), nullable=True),
        sa.Column("risk_validation", JSONB(), nullable=True),
        sa.Column("outcome", JSONB(), nullable=True),
        sa.Column("comparison_notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_historical_replay_snapshots_replay_run_id", "historical_replay_snapshots", ["replay_run_id"])

    # 21. backtest_runs
    op.create_table(
        "backtest_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("config_snapshot", JSONB(), nullable=False),
        sa.Column("fee_model", sa.String(32), nullable=True),
        sa.Column("slippage_model", sa.String(32), nullable=True),
        sa.Column("funding_model", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
    )
    op.create_index("ix_backtest_runs_bot_run_id", "backtest_runs", ["bot_run_id"])

    # 22. backtest_results
    op.create_table(
        "backtest_results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("backtest_run_id", UUID(as_uuid=False), sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losing_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("total_fees", sa.Float(), nullable=True),
        sa.Column("total_funding", sa.Float(), nullable=True),
        sa.Column("sharpe_ratio", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("avg_trade_duration_sec", sa.Float(), nullable=True),
        sa.Column("best_trade_pnl", sa.Float(), nullable=True),
        sa.Column("worst_trade_pnl", sa.Float(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
    )
    op.create_index("ix_backtest_results_backtest_run_id", "backtest_results", ["backtest_run_id"])

    # 23. news_context
    op.create_table(
        "news_context",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(16), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("raw_data", JSONB(), nullable=True),
    )
    op.create_index("ix_news_context_bot_run_id", "news_context", ["bot_run_id"])
    op.create_index("ix_news_context_symbol_timestamp", "news_context", ["symbol", "timestamp"])

    # 24. token_usage
    op.create_table(
        "token_usage",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("model_request_id", UUID(as_uuid=False), sa.ForeignKey("model_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
    )
    op.create_index("ix_token_usage_bot_run_id", "token_usage", ["bot_run_id"])

    # 25. system_events
    op.create_table(
        "system_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="INFO"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", JSONB(), nullable=True),
    )
    op.create_index("ix_system_events_bot_run_id", "system_events", ["bot_run_id"])
    op.create_index("ix_system_events_severity", "system_events", ["severity"])

    # 26. errors
    op.create_table(
        "errors",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("module", sa.String(128), nullable=True),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column("recovered", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_errors_bot_run_id", "errors", ["bot_run_id"])
    op.create_index("ix_errors_error_type", "errors", ["error_type"])

    # 27. kill_switch_events
    op.create_table(
        "kill_switch_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("bot_run_id", UUID(as_uuid=False), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("trigger_reason", sa.Text(), nullable=False),
        sa.Column("state_before", sa.String(32), nullable=False),
        sa.Column("action_taken", sa.String(64), nullable=False),
        sa.Column("positions_closed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_cancelled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requires_manual_review", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_kill_switch_events_bot_run_id", "kill_switch_events", ["bot_run_id"])


def downgrade() -> None:
    # Drop en orden inverso respetando FKs
    op.drop_table("kill_switch_events")
    op.drop_table("errors")
    op.drop_table("system_events")
    op.drop_table("token_usage")
    op.drop_table("news_context")
    op.drop_table("backtest_results")
    op.drop_table("backtest_runs")
    op.drop_table("historical_replay_snapshots")
    op.drop_table("historical_replay_runs")
    op.drop_table("strategy_performance")
    op.drop_table("position_events")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("trades")
    op.drop_table("risk_validations")
    op.drop_table("decision_aggregations")
    op.drop_table("decisions")
    op.drop_table("model_responses")
    op.drop_table("model_requests")
    op.drop_table("feature_packages")
    op.drop_table("volatility_assessments")
    op.drop_table("market_regimes")
    op.drop_table("quant_signals")
    op.drop_table("market_snapshots")
    op.drop_table("accounts_state")
    op.drop_table("bot_state")
    op.drop_table("bot_runs")
