from backend.backtesting.engine import BacktestingEngine, SignalProvider
from backend.backtesting.fee_model import FeeModel, OrderType
from backend.backtesting.funding_model import compute_funding_payment
from backend.backtesting.latency_model import LatencyModel
from backend.backtesting.metrics import compute_backtest_metrics
from backend.backtesting.schemas import (
    BacktestConfig,
    BacktestRunResult,
    CandleRow,
    ClosedTrade,
    DatasetSplit,
    OpenPosition,
    SplitBacktestResult,
    TradeSignal,
    WalkForwardFold,
    WalkForwardFoldResult,
    WalkForwardResult,
)
from backend.backtesting.slippage_model import SlippageModel
from backend.backtesting.validation import (
    assert_history_immutable,
    assert_no_parameter_snooping,
    run_split_backtest,
    run_walk_forward_backtest,
    split_dataset,
    walk_forward_splits,
)

__all__ = [
    "BacktestingEngine",
    "SignalProvider",
    "FeeModel",
    "OrderType",
    "compute_funding_payment",
    "LatencyModel",
    "compute_backtest_metrics",
    "BacktestConfig",
    "BacktestRunResult",
    "CandleRow",
    "ClosedTrade",
    "DatasetSplit",
    "OpenPosition",
    "SplitBacktestResult",
    "TradeSignal",
    "WalkForwardFold",
    "WalkForwardFoldResult",
    "WalkForwardResult",
    "SlippageModel",
    "assert_history_immutable",
    "assert_no_parameter_snooping",
    "run_split_backtest",
    "run_walk_forward_backtest",
    "split_dataset",
    "walk_forward_splits",
]
