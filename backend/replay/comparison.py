"""Comparador de decisiones históricas vs pipeline actual (F11, [86]).

Ejecuta el HistoricalReplayEngine sobre una ventana de snapshots y compara
el resultado de cada paso contra las decisiones que quedaron registradas en DB
en su momento ("entonces" = DB, "ahora" = replay con código actual).

El objetivo es detectar si cambios en el aggregator, el risk engine o las
señales cuantitativas habrían producido resultados distintos para datos pasados.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.replay.historical_replay_engine import DecisionProvider, HistoricalReplayEngine
from backend.replay.schemas import (
    ComparisonReport,
    ComparisonStepResult,
    FieldDelta,
    SnapshotWindow,
)
from backend.storage.models import Decision, DecisionAggregation, RiskValidation
from backend.storage.repositories import (
    DecisionAggregationRepository,
    DecisionRepository,
    RiskValidationRepository,
)

_MATCH_TOLERANCE = timedelta(minutes=5)


def _fmt(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, Decimal):
        return f"{value:.6f}"
    return str(value)


def _build_deltas(
    historical_action: str | None,
    historical_final_action: str | None,
    historical_aggregated_score: float | None,
    historical_risk_result: str | None,
    replay_action: str,
    replay_final_action: str,
    replay_aggregated_score: float,
    replay_risk_result: str,
) -> list[FieldDelta]:
    pairs: list[tuple[str, object, object]] = [
        ("action", historical_action, replay_action),
        ("final_action", historical_final_action, replay_final_action),
        ("aggregated_score", historical_aggregated_score, replay_aggregated_score),
        ("risk_result", historical_risk_result, replay_risk_result),
    ]
    deltas: list[FieldDelta] = []
    for field, entonces, ahora in pairs:
        entonces_str = _fmt(entonces)
        ahora_str = _fmt(ahora)
        # Para floats, comparamos con tolerancia de 1e-6 para evitar falsos positivos por redondeo
        if field == "aggregated_score" and entonces is not None:
            changed = abs(float(str(entonces)) - float(str(ahora))) > 1e-6
        else:
            changed = entonces_str != ahora_str
        deltas.append(
            FieldDelta(field=field, entonces=entonces_str, ahora=ahora_str, changed=changed)
        )
    return deltas


def _find_nearest[T](records: list[T], target: datetime) -> T | None:
    """Devuelve el registro cuyo .timestamp es más cercano a target, dentro de _MATCH_TOLERANCE.

    Cada elemento de records debe tener un atributo `timestamp: datetime`.
    """
    best: T | None = None
    best_delta = _MATCH_TOLERANCE + timedelta(seconds=1)

    for record in records:
        ts: datetime = record.timestamp  # type: ignore[attr-defined]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        delta = abs(ts - target)
        if delta < best_delta:
            best_delta = delta
            best = record

    return best


class DecisionComparator:
    """Compara decisiones históricas registradas en DB contra el pipeline actual.

    Uso típico:

        comparator = DecisionComparator(session)
        report = comparator.compare(window, decision_provider, bot_run_id=run_id)
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._engine = HistoricalReplayEngine(session)
        self._decision_repo = DecisionRepository(session)
        self._aggregation_repo = DecisionAggregationRepository(session)
        self._risk_repo = RiskValidationRepository(session)

    def compare(
        self,
        window: SnapshotWindow,
        decision_provider: DecisionProvider,
        *,
        bot_run_id: str | None = None,
        daily_loss_usdt: Decimal = Decimal("0"),
        total_loss_usdt: Decimal = Decimal("0"),
    ) -> ComparisonReport:
        """Ejecuta el replay y compara cada paso contra los registros históricos en DB.

        Args:
            window: Símbolo y rango temporal a analizar.
            decision_provider: Proveedor de decisiones para el replay (por lo general,
                un provider que devuelve las decisiones históricas de GPT tal como
                fueron registradas, para aislar los cambios en aggregator/risk engine).
            bot_run_id: Si se provee, filtra tanto los snapshots como los registros
                históricos al run indicado.
            daily_loss_usdt: Pérdida diaria acumulada para el risk engine del replay.
            total_loss_usdt: Pérdida total acumulada para el risk engine del replay.
        """
        replay_steps = self._engine.run(
            window,
            decision_provider,
            bot_run_id=bot_run_id,
            daily_loss_usdt=daily_loss_usdt,
            total_loss_usdt=total_loss_usdt,
        )

        historical_decisions = self._decision_repo.list_by_symbol_and_range(
            symbol=window.symbol,
            period_start=window.period_start,
            period_end=window.period_end,
            bot_run_id=bot_run_id,
        )
        historical_aggregations = self._aggregation_repo.list_by_symbol_and_range(
            symbol=window.symbol,
            period_start=window.period_start,
            period_end=window.period_end,
            bot_run_id=bot_run_id,
        )
        historical_risks = self._risk_repo.list_by_symbol_and_range(
            symbol=window.symbol,
            period_start=window.period_start,
            period_end=window.period_end,
            bot_run_id=bot_run_id,
        )

        step_results: list[ComparisonStepResult] = []
        changed_steps = 0
        missing_steps = 0

        for step in replay_steps:
            snapshot_ts: datetime = step.snapshot.timestamp_utc
            if snapshot_ts.tzinfo is None:
                snapshot_ts = snapshot_ts.replace(tzinfo=UTC)

            hist_decision: Decision | None = _find_nearest(historical_decisions, snapshot_ts)
            hist_aggregation: DecisionAggregation | None = _find_nearest(
                historical_aggregations, snapshot_ts
            )
            hist_risk: RiskValidation | None = _find_nearest(historical_risks, snapshot_ts)

            # Un step se considera sin datos históricos cuando faltan decision+aggregation.
            # hist_risk puede estar ausente independientemente (p.ej. si el aggregator bloqueó
            # antes del risk check), así que no entra en esta condición.
            historical_missing = hist_decision is None and hist_aggregation is None

            historical_action: str | None = hist_decision.action if hist_decision else None
            historical_final_action: str | None = (
                hist_aggregation.final_action if hist_aggregation else None
            )
            historical_aggregated_score: float | None = (
                hist_aggregation.aggregated_score if hist_aggregation else None
            )
            historical_risk_result: str | None = hist_risk.result if hist_risk else None

            replay_action = step.decision.decision.value
            replay_final_action = step.aggregation.final_action.value
            replay_aggregated_score = step.aggregation.aggregated_score
            replay_risk_result = step.risk_result.decision.value

            deltas = _build_deltas(
                historical_action,
                historical_final_action,
                historical_aggregated_score,
                historical_risk_result,
                replay_action,
                replay_final_action,
                replay_aggregated_score,
                replay_risk_result,
            )
            any_changed = any(d.changed for d in deltas if not historical_missing)

            step_result = ComparisonStepResult(
                snapshot_id=str(step.snapshot.snapshot_id),
                snapshot_timestamp=snapshot_ts,
                historical_action=historical_action,
                historical_final_action=historical_final_action,
                historical_aggregated_score=historical_aggregated_score,
                historical_risk_result=historical_risk_result,
                historical_missing=historical_missing,
                replay_action=replay_action,
                replay_final_action=replay_final_action,
                replay_aggregated_score=replay_aggregated_score,
                replay_risk_result=replay_risk_result,
                deltas=deltas,
                any_changed=any_changed,
            )
            step_results.append(step_result)

            if historical_missing:
                missing_steps += 1
            elif any_changed:
                changed_steps += 1

        comparable_steps = len(step_results) - missing_steps
        change_rate = changed_steps / comparable_steps if comparable_steps > 0 else 0.0

        return ComparisonReport(
            window=window,
            bot_run_id=bot_run_id,
            total_steps=len(step_results),
            changed_steps=changed_steps,
            missing_steps=missing_steps,
            change_rate=change_rate,
            steps=step_results,
        )
