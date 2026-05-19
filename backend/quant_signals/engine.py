"""Quant Signals Engine — orquesta el cálculo de señales cuantitativas (épica F5)."""

from __future__ import annotations

from backend.market_data.schemas import MarketSnapshot
from backend.quant_signals.momentum import MOMENTUM_TIMEFRAMES, calculate_momentum
from backend.quant_signals.order_flow import calculate_order_flow_imbalance
from backend.quant_signals.schemas import QuantSignalsPackage


def compute_quant_signals(snapshot: MarketSnapshot) -> QuantSignalsPackage:
    """Calcula las señales cuantitativas disponibles para un MarketSnapshot.

    Las señales aún no implementadas quedan en None: el contrato
    QuantSignalsPackage lo permite y los módulos downstream deben tolerarlo.
    """
    momentum_result = calculate_momentum(snapshot)
    ofi_result = calculate_order_flow_imbalance(snapshot)

    return QuantSignalsPackage(
        snapshot_id=snapshot.snapshot_id,
        timestamp_utc=snapshot.timestamp_utc,
        symbol=snapshot.symbol,
        timeframes_used=list(MOMENTUM_TIMEFRAMES),
        momentum_signal=momentum_result.signal,
        order_flow_imbalance_signal=ofi_result.signal,
        raw_feature_refs={
            "momentum": momentum_result.rationale,
            "order_flow_imbalance": ofi_result.rationale,
        },
    )
