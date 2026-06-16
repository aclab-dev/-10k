"""Cálculo de métricas de replay (F11, [87]).

Las métricas se calculan a partir de la lista de ReplayStepResult que produce
HistoricalReplayEngine.run(). Este módulo no escribe en DB; la persistencia
ocurre en ReplayPersistenceService.

Métricas computadas:
- executed_steps: pasos aprobados por el Risk Engine (APPROVE / ADJUST_DOWN)
- signals_activated: pasos con quant_score > 0.5
- directional_wins: pasos ejecutados cuyo siguiente snapshot confirmó la dirección
- win_rate: directional_wins / comparable_executed_steps
- max_drawdown: caída máxima pico-a-valle en la serie aggregated_score
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.decision_engine.schemas import DecisionType
from backend.replay.historical_replay_engine import ReplayStepResult
from backend.risk_engine.schemas import RiskDecision

_EXECUTION_DECISIONS = frozenset({RiskDecision.APPROVE, RiskDecision.ADJUST_DOWN})
_SIGNAL_THRESHOLD = 0.5


@dataclass(frozen=True)
class ReplayMetrics:
    """Métricas calculadas sobre un replay completo."""

    total_steps: int
    executed_steps: int
    signals_activated: int
    directional_wins: int
    comparable_executed_steps: int
    win_rate: float | None
    max_drawdown: float


def compute_replay_metrics(results: list[ReplayStepResult]) -> ReplayMetrics:
    """Calcula métricas de replay a partir de la lista de ReplayStepResult."""
    if not results:
        return ReplayMetrics(
            total_steps=0,
            executed_steps=0,
            signals_activated=0,
            directional_wins=0,
            comparable_executed_steps=0,
            win_rate=None,
            max_drawdown=0.0,
        )

    executed_steps = 0
    signals_activated = 0
    directional_wins = 0
    comparable_executed_steps = 0

    for i, step in enumerate(results):
        if step.aggregation.contributing_sources.quant_score > _SIGNAL_THRESHOLD:
            signals_activated += 1

        is_executed = (
            step.decision.execute
            and step.decision.decision != DecisionType.NO_OPERAR
            and step.risk_result.decision in _EXECUTION_DECISIONS
        )
        if is_executed:
            executed_steps += 1
            if i + 1 < len(results):
                comparable_executed_steps += 1
                next_price = results[i + 1].snapshot.last_price
                entry = Decimal(str(step.decision.entry_price))
                if step.decision.decision == DecisionType.LONG and next_price > entry:
                    directional_wins += 1
                elif step.decision.decision == DecisionType.SHORT and next_price < entry:
                    directional_wins += 1

    win_rate = (
        directional_wins / comparable_executed_steps if comparable_executed_steps > 0 else None
    )
    max_drawdown = _compute_max_drawdown([s.aggregation.aggregated_score for s in results])

    return ReplayMetrics(
        total_steps=len(results),
        executed_steps=executed_steps,
        signals_activated=signals_activated,
        directional_wins=directional_wins,
        comparable_executed_steps=comparable_executed_steps,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
    )


def _compute_max_drawdown(scores: list[float]) -> float:
    """Caída máxima pico-a-valle sobre una serie de scores en [0, 1]."""
    if len(scores) < 2:
        return 0.0
    peak = scores[0]
    max_dd = 0.0
    for score in scores[1:]:
        if score > peak:
            peak = score
        elif peak > 0:
            dd = (peak - score) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd
