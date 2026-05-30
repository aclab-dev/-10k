"""Decision Aggregator — sección 4.10.8 del PDF maestro.

Capa determinística que combina los outputs de las épicas upstream:
  - F5: QuantSignalsPackage (señales cuantitativas)
  - F6: MarketRegimeAssessment + VolatilityAssessmentPackage
  - F7: ModelDecision (evaluación contextual de GPT)

Regla central: si Quant y GPT están en desacuerdo direccional → NO_OPERAR.
GPT nunca tiene autoridad sobre esta capa; el Risk Engine tiene la última palabra.

## Ponderación del aggregated_score

    aggregated_score = (
        0.40 * quant_score        # edge cuantitativo, fuente primaria
      + 0.30 * gpt_context_score  # evaluación contextual GPT
      + 0.20 * regime_factor      # adecuación del régimen al trade propuesto
      + 0.10 * volatility_factor  # factor de entorno de volatilidad
    )

Los pesos reflejan la filosofía del sistema: el edge surge de las señales
cuantitativas (F5/F6) y GPT actúa como evaluador contextual, no como edge.

## Detección de contradicciones

Las siguientes condiciones generan entradas en contradictions_detected.
Si al menos una es verdadera Y GPT propone LONG/SHORT → final_action = NO_OPERAR:

1. direction_mismatch: la señal neta quant (promedio de momentum + mean_reversion
   + breakout, ignorando None) contradice la dirección propuesta por GPT.
   Umbral: net_signal < -0.20 con GPT=LONG, o net_signal > 0.20 con GPT=SHORT.

2. quant_internal_conflict: signal_conflict_score > 0.60. Las señales cuantitativas
   están internamente divididas; operar en ese contexto viola el principio de edge
   reproducible.

3. low_quant_strength: GPT propone LONG/SHORT pero signal_strength_score < 0.40.
   No hay suficiente intensidad cuantitativa que respalde el trade.

4. low_gpt_confidence: ModelDecision.confidence < 0.70. El evaluador contextual
   tiene baja confianza en su propia evaluación.

## Score de régimen (regime_factor)

Regímenes favorables para operar → factor alto:
  - TRENDING / BREAKOUT: 0.85
  - RANGING: 0.55
  - HIGH_VOLATILITY: 0.40
  - LOW_VOLATILITY: 0.50
  - UNCLEAR: 0.20

## Score de volatilidad (volatility_factor)

Directo de VolatilityAssessmentPackage.volatility_score, invertido para
penalizar alta volatilidad cuando es extrema:
  - volatility_score ≤ 0.70 → volatility_factor = 1.0 - volatility_score * 0.30
  - volatility_score > 0.70 → volatility_factor = 1.0 - volatility_score * 0.60
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.decision_engine.aggregator_schemas import (
    ContributingSources,
    DecisionAggregationResult,
)
from backend.decision_engine.schemas import DecisionType, ModelDecision
from backend.market_regime.schemas import MarketRegimeAssessment, PrimaryRegime
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.volatility.schemas import VolatilityAssessmentPackage

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Pesos del aggregated_score (deben sumar 1.0)
_W_QUANT: float = 0.40
_W_GPT: float = 0.30
_W_REGIME: float = 0.20
_W_VOLATILITY: float = 0.10

# Umbral mínimo de aggregated_score para operar
_MIN_SCORE_TO_TRADE: float = 0.50

# Umbral de señal neta quant para detectar dirección
_DIRECTION_MISMATCH_THRESHOLD: float = 0.20

# Umbrales de contradicción
_MAX_CONFLICT_SCORE: float = 0.60
_MIN_STRENGTH_SCORE: float = 0.40
_MIN_GPT_CONFIDENCE: float = 0.70

# Factor de régimen por PrimaryRegime
_REGIME_FACTOR: dict[PrimaryRegime, float] = {
    PrimaryRegime.TRENDING: 0.85,
    PrimaryRegime.BREAKOUT: 0.85,
    PrimaryRegime.RANGING: 0.55,
    PrimaryRegime.LOW_VOLATILITY: 0.50,
    PrimaryRegime.HIGH_VOLATILITY: 0.40,
    PrimaryRegime.UNCLEAR: 0.20,
}


# ---------------------------------------------------------------------------
# DecisionAggregator
# ---------------------------------------------------------------------------


class DecisionAggregator:
    """Agrega los outputs de F5/F6/F7 en una DecisionAggregationResult determinística."""

    def aggregate(
        self,
        gpt_decision: ModelDecision,
        quant_signals: QuantSignalsPackage,
        regime: MarketRegimeAssessment,
        volatility: VolatilityAssessmentPackage,
    ) -> DecisionAggregationResult:
        """Combina inputs upstream y produce un resultado de agregación auditable.

        Si GPT dice NO_OPERAR, el resultado es siempre NO_OPERAR sin detectar
        contradicciones direccionales (la decisión ya está tomada).

        Args:
            gpt_decision: Decisión validada del GPT Context Evaluator.
            quant_signals: Paquete de señales cuantitativas del ciclo actual.
            regime: Clasificación de régimen del ciclo actual.
            volatility: Evaluación de volatilidad del ciclo actual.

        Returns:
            DecisionAggregationResult inmutable con aggregated_score, final_action
            y lista de contradicciones detectadas.
        """
        quant_score = self._compute_quant_score(quant_signals)
        gpt_context_score = gpt_decision.confidence
        regime_factor = _REGIME_FACTOR[regime.primary_regime]
        volatility_factor = self._compute_volatility_factor(volatility)

        aggregated_score = (
            _W_QUANT * quant_score
            + _W_GPT * gpt_context_score
            + _W_REGIME * regime_factor
            + _W_VOLATILITY * volatility_factor
        )

        if gpt_decision.decision == DecisionType.NO_OPERAR:
            # GPT ya decidió no operar; no hay dirección que contradecir.
            contradictions: list[str] = []
            final_action = DecisionType.NO_OPERAR
        else:
            contradictions = self._detect_contradictions(gpt_decision, quant_signals)
            final_action = self._determine_final_action(
                gpt_decision.decision, aggregated_score, contradictions
            )

        return DecisionAggregationResult(
            aggregation_id=str(uuid.uuid4()),
            decision_id=gpt_decision.decision_id,
            symbol=gpt_decision.symbol,
            timestamp_utc=datetime.now(UTC),
            contributing_sources=ContributingSources(
                quant_score=quant_score,
                gpt_context_score=gpt_context_score,
                regime_factor=regime_factor,
                volatility_factor=volatility_factor,
            ),
            aggregated_score=round(aggregated_score, 6),
            final_action=final_action,
            contradictions_detected=contradictions,
        )

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_quant_score(qs: QuantSignalsPackage) -> float:
        """Score cuantitativo: usa signal_strength_score si disponible.

        Fallback: promedio de la intensidad absoluta de señales individuales
        disponibles, normalizado a [0.0, 1.0].
        """
        if qs.signal_strength_score is not None:
            return qs.signal_strength_score

        individual = [
            qs.momentum_signal,
            qs.mean_reversion_signal,
            qs.breakout_signal,
            qs.funding_signal,
            qs.open_interest_signal,
            qs.order_flow_imbalance_signal,
            qs.liquidity_sweep_signal,
        ]
        available = [abs(v) for v in individual if v is not None]
        if not available:
            return 0.0
        return min(sum(available) / len(available), 1.0)

    @staticmethod
    def _compute_volatility_factor(va: VolatilityAssessmentPackage) -> float:
        """Factor de volatilidad: penaliza entornos de alta volatilidad extrema.

        Alta volatilidad implica riesgo de slippage y liquidación. Se penaliza
        progresivamente: ligera penalización para volatilidad normal/alta,
        penalización fuerte para volatilidad extrema (> 0.70).
        """
        score = va.volatility_score
        if score <= 0.70:
            return 1.0 - score * 0.30
        return max(1.0 - score * 0.60, 0.0)

    @staticmethod
    def _net_quant_direction(qs: QuantSignalsPackage) -> float | None:
        """Dirección neta de las señales cuantitativas direccionales.

        Promedia momentum, mean_reversion y breakout (los tres señalan
        dirección de precio). Devuelve None si ninguno está disponible.
        """
        directional = [qs.momentum_signal, qs.mean_reversion_signal, qs.breakout_signal]
        available = [v for v in directional if v is not None]
        if not available:
            return None
        return sum(available) / len(available)

    def _detect_contradictions(
        self,
        gpt_decision: ModelDecision,
        qs: QuantSignalsPackage,
    ) -> list[str]:
        """Detecta conflictos lógicos entre GPT y señales cuantitativas.

        Solo se llama cuando gpt_decision.decision es LONG o SHORT.
        """
        found: list[str] = []

        # 1. Contradicción de dirección
        net = self._net_quant_direction(qs)
        if net is not None:
            if gpt_decision.decision == DecisionType.LONG and net < -_DIRECTION_MISMATCH_THRESHOLD:
                found.append(
                    f"direction_mismatch: GPT=LONG pero señal neta quant={net:.3f} (bearish)"
                )
            elif (
                gpt_decision.decision == DecisionType.SHORT and net > _DIRECTION_MISMATCH_THRESHOLD
            ):
                found.append(
                    f"direction_mismatch: GPT=SHORT pero señal neta quant={net:.3f} (bullish)"
                )

        # 2. Conflicto interno quant
        if qs.signal_conflict_score is not None and qs.signal_conflict_score > _MAX_CONFLICT_SCORE:
            found.append(
                f"quant_internal_conflict: signal_conflict_score={qs.signal_conflict_score:.3f}"
                f" > {_MAX_CONFLICT_SCORE}"
            )

        # 3. Intensidad quant insuficiente
        strength = qs.signal_strength_score
        if strength is not None and strength < _MIN_STRENGTH_SCORE:
            found.append(
                f"low_quant_strength: signal_strength_score={strength:.3f} < {_MIN_STRENGTH_SCORE}"
            )

        # 4. Baja confianza GPT
        if gpt_decision.confidence < _MIN_GPT_CONFIDENCE:
            found.append(
                f"low_gpt_confidence: confidence={gpt_decision.confidence:.3f}"
                f" < {_MIN_GPT_CONFIDENCE}"
            )

        return found

    @staticmethod
    def _determine_final_action(
        gpt_action: DecisionType,
        aggregated_score: float,
        contradictions: list[str],
    ) -> DecisionType:
        """Regla determinística para la acción final.

        - Contradicción detectada → NO_OPERAR (prioritario)
        - aggregated_score < umbral mínimo → NO_OPERAR
        - Otherwise → respeta la decisión de GPT
        """
        if contradictions:
            return DecisionType.NO_OPERAR
        if aggregated_score < _MIN_SCORE_TO_TRADE:
            return DecisionType.NO_OPERAR
        return gpt_action
