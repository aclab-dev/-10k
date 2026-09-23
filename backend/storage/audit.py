"""Audit helpers — write auditable events to DB and emit structured logs.

Public functions, one per event kind:
  audit_decision             → decisions table (evento genérico)
  audit_model_decision       → decisions table (desde un ModelDecision de GPT)
  audit_decision_aggregation → decision_aggregations table
  audit_risk_validation      → risk_validations table
  audit_snapshot             → market_snapshots table
  audit_error                → errors table

Use audit_context() to set a correlation_id for the block's duration via a
stdlib ContextVar. This guarantees async-safe isolation: each asyncio coroutine
carries its own copy, so concurrent coroutines cannot cross-contaminate IDs.

Correlation IDs are also stored in the JSON payload of each record under
``_meta.correlation_id`` for DB-level queries.

Usage::

    from backend.storage.audit import audit_context, audit_decision, audit_error, audit_snapshot

    with audit_context() as cid:
        snap = audit_snapshot(db, bot_run_id=run_id, ...)
        dec  = audit_decision(db, bot_run_id=run_id, ...)

Helpers read correlation_id from the ContextVar automatically when inside
an audit_context block; pass it explicitly to override.
"""

from __future__ import annotations

import copy
import traceback as _traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.orm import Session

from backend.decision_engine.schemas import DecisionType
from backend.storage.models import (
    Decision,
    DecisionAggregation,
    ErrorRecord,
    MarketSnapshot,
    ModelRequest,
    ModelResponse,
    RiskValidation,
)

if TYPE_CHECKING:
    from backend.decision_engine.aggregator_schemas import DecisionAggregationResult
    from backend.decision_engine.schemas import ModelDecision
    from backend.risk_engine.schemas import RiskValidationResult

_log = structlog.get_logger(__name__)

# Async-safe: asyncio creates a copy of the context per coroutine, so
# setting this ContextVar in one coroutine never leaks into another.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def audit_context(correlation_id: str | None = None) -> Iterator[str]:
    """Set *correlation_id* as the active correlation ID for the block's duration.

    Uses a stdlib ContextVar with token-based reset — safe to nest and
    async-safe: each coroutine carries its own copy of the context.

    Generates a UUID v4 if *correlation_id* is not provided. Yields the
    active id so callers can forward it to audit_* helpers explicitly.
    """
    cid = correlation_id or str(uuid.uuid4())
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def audit_decision(
    session: Session,
    *,
    bot_run_id: str,
    correlation_id: str | None = None,
    symbol: str,
    action: str,
    timestamp: datetime | None = None,
    direction: str | None = None,
    confidence: float | None = None,
    margin_usdt: Decimal | None = None,
    leverage: int | None = None,
    stop_loss: Decimal | None = None,
    take_profit: Decimal | None = None,
    reasoning: str | None = None,
    model_response_id: str | None = None,
    raw_decision: dict[str, Any] | None = None,
) -> Decision:
    """Persist a decision event and emit a structured log.

    *correlation_id* defaults to the value set by the enclosing audit_context;
    falls back to a fresh UUID if called outside one.
    It is stored in ``raw_decision._meta.correlation_id``.
    """
    cid = correlation_id or _correlation_id.get() or str(uuid.uuid4())
    payload: dict[str, Any] = copy.deepcopy(raw_decision) if raw_decision else {}
    payload.setdefault("_meta", {})["correlation_id"] = cid

    record = Decision(
        bot_run_id=bot_run_id,
        model_response_id=model_response_id,
        symbol=symbol,
        timestamp=timestamp or _now(),
        action=action,
        direction=direction,
        confidence=confidence,
        margin_usdt=margin_usdt,
        leverage=leverage,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reasoning=reasoning,
        raw_decision=payload,
    )
    session.add(record)
    session.flush()

    _log.info(
        "audit.decision",
        decision_id=record.id,
        bot_run_id=bot_run_id,
        correlation_id=cid,
        symbol=symbol,
        action=action,
        direction=direction,
        confidence=confidence,
    )
    return record


def audit_snapshot(
    session: Session,
    *,
    bot_run_id: str,
    correlation_id: str | None = None,
    symbol: str,
    timestamp: datetime,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    funding_rate: float | None = None,
    open_interest: Decimal | None = None,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    spread: Decimal | None = None,
    extra: dict[str, Any] | None = None,
) -> MarketSnapshot:
    """Persist a market snapshot and emit a structured log.

    *correlation_id* defaults to the value set by the enclosing audit_context;
    falls back to a fresh UUID if called outside one.
    It is stored in ``extra._meta.correlation_id``.
    """
    cid = correlation_id or _correlation_id.get() or str(uuid.uuid4())
    meta: dict[str, Any] = copy.deepcopy(extra) if extra else {}
    meta.setdefault("_meta", {})["correlation_id"] = cid

    record = MarketSnapshot(
        bot_run_id=bot_run_id,
        symbol=symbol,
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        funding_rate=funding_rate,
        open_interest=open_interest,
        bid=bid,
        ask=ask,
        spread=spread,
        extra=meta,
    )
    session.add(record)
    session.flush()

    _log.info(
        "audit.snapshot",
        snapshot_id=record.id,
        bot_run_id=bot_run_id,
        correlation_id=cid,
        symbol=symbol,
        close=str(close),
    )
    return record


def audit_model_request(
    session: Session,
    *,
    bot_run_id: str,
    symbol: str,
    model: str,
    context: dict[str, Any],
    request_hash: str,
    feature_package_id: str | None = None,
    timestamp: datetime | None = None,
    correlation_id: str | None = None,
) -> ModelRequest:
    """Persist a GPT model request and emit a structured log.

    ``context`` almacena los prompts (system + user) tal como se enviaron
    al modelo. Asegurarse de que no contengan datos sensibles antes de
    llegar a entornos LIVE.
    """
    cid = correlation_id or _correlation_id.get() or str(uuid.uuid4())

    record = ModelRequest(
        bot_run_id=bot_run_id,
        feature_package_id=feature_package_id,
        symbol=symbol,
        timestamp=timestamp or _now(),
        model=model,
        context=context,
        request_hash=request_hash,
    )
    session.add(record)
    session.flush()

    _log.info(
        "audit.model_request",
        model_request_id=record.id,
        bot_run_id=bot_run_id,
        correlation_id=cid,
        symbol=symbol,
        model=model,
        request_hash=request_hash,
    )
    return record


def audit_model_response(
    session: Session,
    *,
    model_request_id: str,
    model: str,
    raw_response: str,
    normalized_response: dict[str, Any] | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    finish_reason: str | None = None,
    is_valid_schema: bool = False,
    timestamp: datetime | None = None,
) -> ModelResponse:
    """Persist a GPT model response and emit a structured log."""
    record = ModelResponse(
        model_request_id=model_request_id,
        timestamp=timestamp or _now(),
        raw_response=raw_response,
        normalized_response=normalized_response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model=model,
        finish_reason=finish_reason,
        is_valid_schema=is_valid_schema,
    )
    session.add(record)
    session.flush()

    _log.info(
        "audit.model_response",
        model_response_id=record.id,
        model_request_id=model_request_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        is_valid_schema=is_valid_schema,
    )
    return record


def audit_error(
    session: Session,
    *,
    bot_run_id: str,
    correlation_id: str | None = None,
    message: str,
    exc: BaseException | None = None,
    module: str | None = None,
    recovered: bool = False,
    details: dict[str, Any] | None = None,
) -> ErrorRecord:
    """Persist an error record and emit a structured log.

    *correlation_id* defaults to the value set by the enclosing audit_context;
    falls back to a fresh UUID if called outside one.
    It is stored in ``details._meta.correlation_id``.
    If *exc* is provided, its type name and full traceback are extracted.
    """
    cid = correlation_id or _correlation_id.get() or str(uuid.uuid4())
    tb: str | None = None
    error_type: str | None = None
    if exc is not None:
        tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        error_type = type(exc).__name__

    extra: dict[str, Any] = copy.deepcopy(details) if details else {}
    extra.setdefault("_meta", {})["correlation_id"] = cid

    record = ErrorRecord(
        bot_run_id=bot_run_id,
        module=module,
        error_type=error_type,
        message=message,
        traceback=tb,
        details=extra,
        recovered=recovered,
    )
    session.add(record)
    session.flush()

    _log.error(
        "audit.error",
        error_id=record.id,
        bot_run_id=bot_run_id,
        correlation_id=cid,
        module=module,
        error_type=error_type,
        message=message,
        recovered=recovered,
        exc_info=exc is not None,
    )
    return record


def audit_model_decision(
    session: Session,
    decision: ModelDecision,
    *,
    bot_run_id: str,
) -> Decision:
    """Persist a ModelDecision (Anexo B, tabla decisions).

    La fila toma el `decision_id` del propio ModelDecision como PK, y no un
    UUID nuevo: `decision_aggregations.decision_id` referencia ese mismo id,
    así que generar otro dejaría la FK sin destino y la agregación no entraría.

    Idempotente por esa misma razón: reusar el id como PK hace que un retry del
    mismo ModelDecision (un segundo tick con la decisión ya vista) choque
    contra la PK y tumbe el ciclo. Si la fila ya existe se devuelve tal cual,
    sin reescribirla — la decisión es inmutable, no hay nada que actualizar.
    """
    existing = session.get(Decision, decision.decision_id)
    if existing is not None:
        _log.info(
            "audit.model_decision.already_persisted",
            decision_id=existing.id,
            bot_run_id=bot_run_id,
        )
        return existing

    record = Decision(
        id=decision.decision_id,
        bot_run_id=bot_run_id,
        symbol=decision.symbol,
        timestamp=decision.timestamp_utc,
        # `action` describe qué se resolvió; `direction` es LONG/SHORT y la
        # columna admite 8 caracteres, así que NO_OPERAR va en action y deja
        # direction en NULL — no tiene dirección que registrar.
        action="OPEN" if decision.execute else "NO_OPERAR",
        direction=(
            decision.decision.value if decision.decision != DecisionType.NO_OPERAR else None
        ),
        confidence=decision.confidence,
        margin_usdt=Decimal(str(decision.margin_usdt)),
        leverage=decision.leverage,
        stop_loss=Decimal(str(decision.stop_loss)),
        take_profit=Decimal(str(decision.take_profit)),
        reasoning=decision.decision_rationale_summary,
        raw_decision=decision.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()

    _log.info(
        "audit.model_decision",
        decision_id=record.id,
        bot_run_id=bot_run_id,
        symbol=record.symbol,
        action=record.action,
        direction=record.direction,
    )
    return record


def audit_decision_aggregation(
    session: Session,
    result: DecisionAggregationResult,
    *,
    bot_run_id: str,
) -> DecisionAggregation:
    """Persist a DecisionAggregationResult (Anexo B, tabla decision_aggregations).

    `decision_id` apunta a `decisions.id`. La columna es nullable y la FK es
    `ondelete=SET NULL`, pero eso sólo describe qué pasa al *borrar* la
    decisión: insertar un id que no existe viola la FK igual. Por eso el caller
    persiste la decisión antes, o no llama a esta función.
    """
    record = DecisionAggregation(**result.to_db_kwargs(bot_run_id))
    session.add(record)
    session.flush()

    _log.info(
        "audit.decision_aggregation",
        aggregation_id=record.id,
        bot_run_id=bot_run_id,
        symbol=record.symbol,
        final_action=record.final_action,
        aggregated_score=record.aggregated_score,
    )
    return record


def audit_risk_validation(
    session: Session,
    result: RiskValidationResult,
    *,
    bot_run_id: str,
    link_aggregation: bool = True,
) -> RiskValidation:
    """Persist a RiskValidationResult (Anexo B, tabla risk_validations).

    Es el destino de `RiskValidationResult.reasons`, donde cada check deja su
    motivo — incluida la estimación de slippage pre-trade de la regla 13. Se
    persiste en los cuatro resultados (APPROVE, ADJUST_DOWN, BLOCK, NO_OPERAR):
    un trade rechazado es tan auditable como uno ejecutado, y sin la fila del
    rechazo no hay forma de revisar después por qué se descartó.

    `link_aggregation=False` guarda la fila con `decision_aggregation_id` en
    NULL. Lo usa el caller que no persistió la agregación — `log_all_decisions`
    apagado con `log_risk_validations` encendido es una combinación válida —:
    apuntar a una agregación inexistente viola la FK al insertar y cuesta el
    ciclo del símbolo entero. Vale más la validación sin vínculo que sin fila.
    """
    kwargs = result.to_db_kwargs(bot_run_id)
    if not link_aggregation:
        kwargs["decision_aggregation_id"] = None
    record = RiskValidation(**kwargs)
    session.add(record)
    session.flush()

    _log.info(
        "audit.risk_validation",
        validation_id=record.id,
        bot_run_id=bot_run_id,
        symbol=record.symbol,
        result=record.result,
        rules=sorted(record.reasons or {}),
    )
    return record
