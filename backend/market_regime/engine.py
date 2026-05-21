"""Market Regime Engine — coordina clasificadores y produce MarketRegimeAssessment.

Entrada:  MarketSnapshot (sección 4.10.1)
Salida:   MarketRegimeAssessment (sección 4.10.3)

Sin estado interno. Toda clasificación es determinista y reproducible dado
el mismo MarketSnapshot.
"""

from __future__ import annotations

from backend.market_data.schemas import MarketSnapshot
from backend.market_regime.classifiers import (
    assess_funding_state,
    assess_liquidity_state,
    assess_open_interest_state,
    assess_trend_alignment,
    assess_volatility_state,
    classify_primary_regime,
    classify_secondary_regime,
)
from backend.market_regime.schemas import MarketRegimeAssessment


class MarketRegimeEngine:
    """Clasifica el régimen de mercado a partir de un MarketSnapshot.

    La instancia no mantiene estado: puede reutilizarse sin reset entre ciclos.
    """

    def assess(self, snapshot: MarketSnapshot) -> MarketRegimeAssessment:
        """Produce una MarketRegimeAssessment para el snapshot dado.

        Pasos:
          1. Evalúa estados auxiliares (volatilidad, liquidez, funding, OI, tendencia).
          2. Determina régimen primario mediante reglas explícitas.
          3. Determina régimen secundario como contexto complementario.
          4. Devuelve el contrato inmutable.
        """
        volatility_state = assess_volatility_state(snapshot)
        liquidity_state = assess_liquidity_state(snapshot)
        funding_state = assess_funding_state(snapshot)
        open_interest_state = assess_open_interest_state(snapshot)
        trend_alignment = assess_trend_alignment(snapshot)

        primary_regime, confidence, notes = classify_primary_regime(
            snapshot, volatility_state, trend_alignment
        )

        secondary_regime = classify_secondary_regime(
            primary_regime, volatility_state, trend_alignment
        )

        return MarketRegimeAssessment(
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            timestamp_utc=snapshot.timestamp_utc,
            primary_regime=primary_regime,
            secondary_regime=secondary_regime,
            regime_confidence=confidence,
            volatility_state=volatility_state,
            liquidity_state=liquidity_state,
            funding_state=funding_state,
            open_interest_state=open_interest_state,
            trend_alignment=trend_alignment,
            notes=notes,
        )
