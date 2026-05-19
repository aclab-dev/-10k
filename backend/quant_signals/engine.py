"""Quant Signals Engine — orquesta el cálculo de señales cuantitativas (épica F5)."""

from __future__ import annotations

from backend.market_data.schemas import MarketSnapshot
from backend.quant_signals.breakout import BREAKOUT_TIMEFRAMES, calculate_breakout
from backend.quant_signals.funding import compute_funding_signal
from backend.quant_signals.liquidity_sweeps import (
    LIQUIDITY_SWEEP_TIMEFRAMES,
    calculate_liquidity_sweeps,
)
from backend.quant_signals.mean_reversion import compute_mean_reversion_signal
from backend.quant_signals.momentum import MOMENTUM_TIMEFRAMES, calculate_momentum
from backend.quant_signals.open_interest import compute_open_interest_signal
from backend.quant_signals.order_flow import ORDER_FLOW_TIMEFRAMES, calculate_order_flow_imbalance
from backend.quant_signals.schemas import QuantSignalsPackage


def compute_quant_signals(snapshot: MarketSnapshot) -> QuantSignalsPackage:
    """Calcula las señales cuantitativas disponibles para un MarketSnapshot.

    Las señales aún no implementadas quedan en None: el contrato
    QuantSignalsPackage lo permite y los módulos downstream deben tolerarlo.
    """
    momentum_result = calculate_momentum(snapshot)
    breakout_result = calculate_breakout(snapshot)
    ofi_result = calculate_order_flow_imbalance(snapshot)
    sweep_result = calculate_liquidity_sweeps(snapshot)

    return QuantSignalsPackage(
        snapshot_id=snapshot.snapshot_id,
        timestamp_utc=snapshot.timestamp_utc,
        symbol=snapshot.symbol,
        timeframes_used=sorted(
            set(MOMENTUM_TIMEFRAMES)
            | set(BREAKOUT_TIMEFRAMES)
            | set(ORDER_FLOW_TIMEFRAMES)
            | set(LIQUIDITY_SWEEP_TIMEFRAMES)
        ),
        momentum_signal=momentum_result.signal,
        mean_reversion_signal=compute_mean_reversion_signal(snapshot),
        breakout_signal=breakout_result.signal,
        funding_signal=compute_funding_signal(snapshot),
        open_interest_signal=compute_open_interest_signal(snapshot),
        order_flow_imbalance_signal=ofi_result.signal,
        liquidity_sweep_signal=sweep_result.signal,
        raw_feature_refs={
            "momentum": momentum_result.rationale,
            "breakout": breakout_result.rationale,
            "order_flow_imbalance": ofi_result.rationale,
            "liquidity_sweeps": sweep_result.rationale,
        },
    )
