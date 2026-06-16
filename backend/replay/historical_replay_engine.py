"""Historical Replay Engine — re-ejecuta el pipeline de decisión sobre datos pasados (F11, [85]).

Recorre cronológicamente los snapshots históricos de una ventana ([84]) y los
hace pasar por las mismas etapas deterministas que el pipeline en vivo:
quant signals -> regime -> volatility -> decisión -> aggregator -> risk engine.

La etapa de decisión (GPT u otra fuente) se inyecta vía `DecisionProvider` en
lugar de llamarse internamente: desacopla el motor de replay de cómo se
obtiene la decisión (llamada real a GPT, decisión histórica ya registrada, o
un stub determinista en tests), igual que ya lo hace el harness E2E de [83].
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from backend.core.config import load_config
from backend.decision_engine.aggregator import DecisionAggregator
from backend.decision_engine.aggregator_schemas import DecisionAggregationResult
from backend.decision_engine.schemas import ModelDecision
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.engine import MarketRegimeEngine
from backend.market_regime.schemas import MarketRegimeAssessment
from backend.quant_signals.engine import compute_quant_signals
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.replay.schemas import SnapshotWindow
from backend.replay.snapshot_loader import SnapshotLoader
from backend.risk_engine import engine as risk_engine
from backend.risk_engine.schemas import RiskValidationResult
from backend.storage.models import MarketSnapshot as MarketSnapshotRow
from backend.volatility.engine import compute_volatility_assessment
from backend.volatility.schemas import VolatilityAssessmentPackage


class DecisionProvider(Protocol):
    """Provee el ModelDecision para un snapshot histórico y sus quant signals."""

    def __call__(
        self, snapshot: MarketSnapshot, quant_signals: QuantSignalsPackage, /
    ) -> ModelDecision: ...


@dataclass(frozen=True)
class ReplayStepResult:
    """Resultado de re-ejecutar el pipeline sobre un snapshot histórico."""

    snapshot: MarketSnapshot
    quant_signals: QuantSignalsPackage
    regime: MarketRegimeAssessment
    volatility: VolatilityAssessmentPackage
    decision: ModelDecision
    aggregation: DecisionAggregationResult
    risk_result: RiskValidationResult


def snapshot_row_to_pydantic(row: MarketSnapshotRow) -> MarketSnapshot:
    """Reconstruye el MarketSnapshot Pydantic desde el registro ORM de market_snapshots.

    Inverso de `MarketSnapshot.to_db_kwargs`. Los campos sin columna propia
    viven en `extra`. `history_*` no se persiste hoy ([84]), así que el
    replay corre sin contexto histórico de velas previas.
    """
    extra = row.extra or {}
    candles_raw = extra["candles"]

    def _candle(tf: str) -> CandleData:
        return CandleData.model_validate(candles_raw[tf])

    if row.bid is None or row.ask is None or row.spread is None:
        raise ValueError(
            f"Snapshot {row.id} no tiene bid/ask/spread persistidos; no se puede reconstruir"
        )

    return MarketSnapshot(
        snapshot_id=row.id,
        timestamp_utc=row.timestamp,
        exchange=Exchange(extra["exchange"]),
        environment=extra["environment"],
        symbol=row.symbol,
        last_price=row.close,
        bid=row.bid,
        ask=row.ask,
        spread_absolute=row.spread,
        spread_percent=Decimal(str(extra["spread_percent"])),
        candles=Candles(
            tf_5m=_candle("tf_5m"),
            tf_15m=_candle("tf_15m"),
            tf_1h=_candle("tf_1h"),
            tf_4h=_candle("tf_4h"),
        ),
        volume=row.volume,
        volatility=extra.get("volatility"),
        funding_rate=row.funding_rate,
        open_interest=row.open_interest,
        account_balance_usdt=Decimal(str(extra["account_balance_usdt"])),
        open_positions_count=extra["open_positions_count"],
        active_orders_count=extra["active_orders_count"],
        latency_ms=extra["latency_ms"],
        exchange_server_time=datetime.fromisoformat(extra["exchange_server_time"]),
        local_time=datetime.fromisoformat(extra["local_time"]),
        clock_skew_ms=extra["clock_skew_ms"],
        data_freshness_status=DataFreshnessStatus(extra["data_freshness_status"]),
        coherence_status=CoherenceStatus(extra["coherence_status"]),
    )


class HistoricalReplayEngine:
    """Re-ejecuta el pipeline completo sobre una ventana de snapshots históricos.

    Reutiliza exactamente las mismas llamadas que el pipeline en vivo
    (quant_signals.engine, market_regime.engine, volatility.engine,
    decision_engine.aggregator, risk_engine.engine) en lugar de duplicar
    su lógica, para garantizar que el resultado sea el que se habría
    producido en ese momento.
    """

    def __init__(self, session: Session) -> None:
        self._loader = SnapshotLoader(session)
        self._regime_engine = MarketRegimeEngine()
        self._aggregator = DecisionAggregator()

    def run(
        self,
        window: SnapshotWindow,
        decision_provider: DecisionProvider,
        *,
        bot_run_id: str | None = None,
        daily_loss_usdt: Decimal = Decimal("0"),
        total_loss_usdt: Decimal = Decimal("0"),
    ) -> list[ReplayStepResult]:
        """Carga los snapshots de la ventana ([84]) y recorre cada uno cronológicamente."""
        rows = self._loader.load(window, bot_run_id=bot_run_id)
        config = load_config()
        results: list[ReplayStepResult] = []

        for row in rows:
            snapshot = snapshot_row_to_pydantic(row)
            quant_signals = compute_quant_signals(snapshot)
            regime = self._regime_engine.assess(snapshot)
            volatility = compute_volatility_assessment(snapshot)
            decision = decision_provider(snapshot, quant_signals)
            aggregation = self._aggregator.aggregate(decision, quant_signals, regime, volatility)
            risk_result = risk_engine.validate(
                aggregation=aggregation,
                decision=decision,
                daily_loss_usdt=daily_loss_usdt,
                total_loss_usdt=total_loss_usdt,
                config=config,
            )
            results.append(
                ReplayStepResult(
                    snapshot=snapshot,
                    quant_signals=quant_signals,
                    regime=regime,
                    volatility=volatility,
                    decision=decision,
                    aggregation=aggregation,
                    risk_result=risk_result,
                )
            )

        return results
