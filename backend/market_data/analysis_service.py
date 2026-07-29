"""MarketAnalysisService — computa y persiste Quant Signals + Regime + Volatility (F5/F6).

Se dispara como hook tras cada MarketSnapshot real validado (F4), para que el
ciclo real produzca `QuantSignalsPackage`/`MarketRegimeAssessment`/
`VolatilityAssessmentPackage` reales por símbolo, no solo mockeados en tests.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from backend.market_data.schemas import MarketSnapshot
from backend.market_regime.engine import MarketRegimeEngine
from backend.quant_signals.engine import compute_quant_signals
from backend.storage.models import MarketRegime, QuantSignal, VolatilityAssessment
from backend.storage.repositories.snapshots import (
    MarketRegimeRepository,
    QuantSignalRepository,
    VolatilityAssessmentRepository,
)
from backend.volatility.engine import compute_volatility_assessment

log = structlog.get_logger(__name__)


class MarketAnalysisService:
    """Deriva y persiste señales cuantitativas, régimen y volatilidad de un snapshot real.

    Aísla sus propias fallas (loguea ERROR, no propaga): pensado para usarse como
    hook de `MarketDataCycleService`, que ya aplica el mismo criterio de
    aislamiento por símbolo (F14/CR) — una falla acá no debe interferir con el
    resto del pipeline de market data del ciclo.
    """

    def __init__(self, session: Session, bot_run_id: str) -> None:
        self._quant_repo = QuantSignalRepository(session)
        self._regime_repo = MarketRegimeRepository(session)
        self._volatility_repo = VolatilityAssessmentRepository(session)
        self._regime_engine = MarketRegimeEngine()
        self._bot_run_id = bot_run_id

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Computa y persiste Quant Signals + Regime + Volatility para este snapshot."""
        try:
            quant = compute_quant_signals(snapshot)
            self._quant_repo.save(QuantSignal(**quant.to_db_kwargs(self._bot_run_id)))

            regime = self._regime_engine.assess(snapshot)
            self._regime_repo.save(MarketRegime(**regime.to_db_kwargs(self._bot_run_id)))

            volatility = compute_volatility_assessment(snapshot)
            self._volatility_repo.save(
                VolatilityAssessment(**volatility.to_db_kwargs(self._bot_run_id))
            )
        except Exception:
            log.error(
                "market_analysis_service.on_snapshot_failed",
                symbol=snapshot.symbol,
                exc_info=True,
            )


__all__ = ["MarketAnalysisService"]
