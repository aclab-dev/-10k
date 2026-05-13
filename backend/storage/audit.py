"""Audit helpers — write auditable events to DB and emit structured logs.

Three public functions cover the event kinds required by F3-DoD:
  audit_decision  → decisions table
  audit_snapshot  → market_snapshots table
  audit_error     → errors table

Use audit_context() to bind a correlation_id to structlog contextvars so
every log line inside the block — not just audit calls — carries the same id.
Correlation IDs are also stored in the JSON payload of each record under
``_meta.correlation_id`` for DB-level queries.

Usage::

    from backend.storage.audit import audit_context, audit_decision, audit_error

    with audit_context() as cid:
        snap = audit_snapshot(db, bot_run_id=run_id, correlation_id=cid, ...)
        dec  = audit_decision(db, bot_run_id=run_id, correlation_id=cid, ...)
"""

from __future__ import annotations

import traceback as _traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterator

import structlog
from sqlalchemy.orm import Session

from backend.storage.models import Decision, ErrorRecord, MarketSnapshot

_log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

@contextmanager
def audit_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind *correlation_id* to structlog contextvars for the block's duration.

    Generates a UUID v4 if none is provided. Yields the active id so callers
    can forward it to audit_* helpers.

    Safe to nest: the previous correlation_id (if any) is restored on exit.
    """
    cid = correlation_id or str(uuid.uuid4())
    previous = structlog.contextvars.get_contextvars().get("correlation_id")
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    try:
        yield cid
    finally:
        if previous is None:
            structlog.contextvars.unbind_contextvars("correlation_id")
        else:
            structlog.contextvars.bind_contextvars(correlation_id=previous)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def audit_decision(
    session: Session,
    *,
    bot_run_id: str,
    correlation_id: str,
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

    *correlation_id* is stored in ``raw_decision._meta.correlation_id``.
    """
    payload: dict[str, Any] = dict(raw_decision or {})
    payload.setdefault("_meta", {})["correlation_id"] = correlation_id

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
        correlation_id=correlation_id,
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
    correlation_id: str,
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

    *correlation_id* is stored in ``extra._meta.correlation_id``.
    """
    meta: dict[str, Any] = dict(extra or {})
    meta.setdefault("_meta", {})["correlation_id"] = correlation_id

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
        correlation_id=correlation_id,
        symbol=symbol,
        close=str(close),
    )
    return record


def audit_error(
    session: Session,
    *,
    bot_run_id: str,
    correlation_id: str,
    message: str,
    exc: BaseException | None = None,
    module: str | None = None,
    recovered: bool = False,
    details: dict[str, Any] | None = None,
) -> ErrorRecord:
    """Persist an error record and emit a structured log.

    *correlation_id* is stored in ``details._meta.correlation_id``.
    If *exc* is provided, its type name and full traceback are extracted.
    """
    tb: str | None = None
    error_type: str | None = None
    if exc is not None:
        tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        error_type = type(exc).__name__

    extra: dict[str, Any] = dict(details or {})
    extra.setdefault("_meta", {})["correlation_id"] = correlation_id

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
        correlation_id=correlation_id,
        module=module,
        error_type=error_type,
        message=message,
        recovered=recovered,
        exc_info=exc,
    )
    return record
