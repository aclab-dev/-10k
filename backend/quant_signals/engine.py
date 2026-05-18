"""Quant Signals Engine — orquesta el cálculo de señales cuantitativas (épica F5)."""

from __future__ import annotations

from backend.market_data.schemas import MarketSnapshot
from backend.quant_signals.momentum import MOMENTUM_TIMEFRAMES, calculate_momentum
from backend.quant_signals.schemas import QuantSignalsPackage


def compute_quant_signals(snapshot: MarketSnapshot) -> QuantSignalsPackage:
    """Calcula las señales cuantitativas disponibles para un MarketSnapshot.

    Las señales aún no implementadas quedan en None: el contrato
    QuantSignalsPackage lo permite y los módulos downstream deben tolerarlo.
    """
    momentum_result = calculate_momentum(snapshot)

    return QuantSignalsPackage(
        snapshot_id=snapshot.snapshot_id,
        timestamp_utc=snapshot.timestamp_utc,
        symbol=snapshot.symbol,
        # Al integrar señales 48-52, este campo debe unir los TFs de todas
        # las señales activas en lugar de reflejar solo los de momentum.
        timeframes_used=list(MOMENTUM_TIMEFRAMES),
        momentum_signal=momentum_result.signal,
        raw_feature_refs={"momentum": momentum_result.rationale},
    )
