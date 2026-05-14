"""Tests unitarios de backend/storage/audit.py.

Usan SQLite en memoria (mismo patrón que test_models.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.storage.audit import (
    _correlation_id,
    audit_context,
    audit_decision,
    audit_error,
    audit_snapshot,
)
from backend.storage.database import Base
from backend.storage.models import BotRun, Decision, ErrorRecord, MarketSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


@pytest.fixture
def bot_run(session: Session) -> BotRun:
    run = BotRun(
        id=str(uuid.uuid4()),
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"mode": "PAPER"},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


def _cid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# audit_context
# ---------------------------------------------------------------------------


class TestAuditContext:
    def test_yields_provided_id(self):
        cid = _cid()
        with audit_context(cid) as returned:
            assert returned == cid

    def test_generates_uuid_when_none(self):
        with audit_context() as cid:
            assert len(cid) == 36  # UUID v4 string

    def test_sets_correlation_id_contextvar(self):
        cid = _cid()
        with audit_context(cid):
            assert _correlation_id.get() == cid

    def test_resets_after_context_exits(self):
        cid = _cid()
        with audit_context(cid):
            pass
        assert _correlation_id.get() is None

    def test_resets_even_on_exception(self):
        cid = _cid()
        with pytest.raises(ValueError):
            with audit_context(cid):
                raise ValueError("boom")
        assert _correlation_id.get() is None

    def test_nested_context_restores_outer_id(self):
        outer = _cid()
        inner = _cid()
        with audit_context(outer):
            with audit_context(inner) as returned_inner:
                assert returned_inner == inner
                assert _correlation_id.get() == inner
            assert _correlation_id.get() == outer
        assert _correlation_id.get() is None


# ---------------------------------------------------------------------------
# audit_decision
# ---------------------------------------------------------------------------


class TestAuditDecision:
    def test_returns_decision_instance(self, session, bot_run):
        cid = _cid()
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            symbol="BTC-USDT",
            action="OPEN",
        )
        assert isinstance(dec, Decision)

    def test_persisted_with_correct_fields(self, session, bot_run):
        cid = _cid()
        ts = datetime.now(UTC)
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            symbol="ETH-USDT",
            action="CLOSE",
            direction="LONG",
            confidence=0.85,
            timestamp=ts,
            reasoning="test",
        )
        assert dec.symbol == "ETH-USDT"
        assert dec.action == "CLOSE"
        assert dec.direction == "LONG"
        assert dec.confidence == pytest.approx(0.85)
        assert dec.timestamp == ts
        assert dec.reasoning == "test"

    def test_correlation_id_stored_in_raw_decision(self, session, bot_run):
        cid = _cid()
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            symbol="BTC-USDT",
            action="NO_OPERAR",
        )
        assert dec.raw_decision["_meta"]["correlation_id"] == cid

    def test_merges_existing_raw_decision_payload(self, session, bot_run):
        cid = _cid()
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            symbol="BTC-USDT",
            action="OPEN",
            raw_decision={"quant_score": 0.9},
        )
        assert dec.raw_decision["quant_score"] == pytest.approx(0.9)
        assert dec.raw_decision["_meta"]["correlation_id"] == cid

    def test_id_is_set_after_flush(self, session, bot_run):
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=_cid(),
            symbol="BTC-USDT",
            action="OPEN",
        )
        assert dec.id is not None

    def test_timestamp_defaults_to_now_when_not_provided(self, session, bot_run):
        before = datetime.now(UTC)
        dec = audit_decision(
            session,
            bot_run_id=bot_run.id,
            correlation_id=_cid(),
            symbol="BTC-USDT",
            action="OPEN",
        )
        after = datetime.now(UTC)
        assert before <= dec.timestamp <= after


# ---------------------------------------------------------------------------
# audit_snapshot
# ---------------------------------------------------------------------------


class TestAuditSnapshot:
    def _snap(self, session: Session, bot_run: BotRun, **kwargs: Any) -> MarketSnapshot:
        defaults: dict[str, Any] = {
            "bot_run_id": bot_run.id,
            "correlation_id": _cid(),
            "symbol": "BTC-USDT",
            "timestamp": datetime.now(UTC),
            "open_price": Decimal("60000"),
            "high": Decimal("61000"),
            "low": Decimal("59000"),
            "close": Decimal("60500"),
            "volume": Decimal("1500"),
        }
        defaults.update(kwargs)
        return audit_snapshot(session, **defaults)

    def test_returns_market_snapshot_instance(self, session, bot_run):
        snap = self._snap(session, bot_run)
        assert isinstance(snap, MarketSnapshot)

    def test_persisted_ohlcv(self, session, bot_run):
        snap = self._snap(
            session,
            bot_run,
            open_price=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=Decimal("500"),
        )
        assert snap.open == Decimal("100")
        assert snap.high == Decimal("110")
        assert snap.low == Decimal("90")
        assert snap.close == Decimal("105")
        assert snap.volume == Decimal("500")

    def test_correlation_id_stored_in_extra(self, session, bot_run):
        cid = _cid()
        snap = self._snap(session, bot_run, correlation_id=cid)
        assert snap.extra["_meta"]["correlation_id"] == cid

    def test_preserves_caller_extra_fields(self, session, bot_run):
        cid = _cid()
        snap = self._snap(session, bot_run, correlation_id=cid, extra={"source": "bingx"})
        assert snap.extra["source"] == "bingx"
        assert snap.extra["_meta"]["correlation_id"] == cid

    def test_optional_fields_none_by_default(self, session, bot_run):
        snap = self._snap(session, bot_run)
        assert snap.funding_rate is None
        assert snap.open_interest is None
        assert snap.bid is None
        assert snap.ask is None


# ---------------------------------------------------------------------------
# audit_error
# ---------------------------------------------------------------------------


class TestAuditError:
    def test_returns_error_record_instance(self, session, bot_run):
        rec = audit_error(
            session,
            bot_run_id=bot_run.id,
            correlation_id=_cid(),
            message="something broke",
        )
        assert isinstance(rec, ErrorRecord)

    def test_extracts_exception_type_and_traceback(self, session, bot_run):
        cid = _cid()
        try:
            raise ValueError("test error")
        except ValueError as exc:
            rec = audit_error(
                session,
                bot_run_id=bot_run.id,
                correlation_id=cid,
                message="test error",
                exc=exc,
                module="tests.unit.test_audit",
            )

        assert rec.error_type == "ValueError"
        assert rec.traceback is not None
        assert "ValueError" in rec.traceback
        assert rec.module == "tests.unit.test_audit"

    def test_correlation_id_stored_in_details(self, session, bot_run):
        cid = _cid()
        rec = audit_error(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            message="oops",
        )
        assert rec.details["_meta"]["correlation_id"] == cid

    def test_preserves_caller_details(self, session, bot_run):
        cid = _cid()
        rec = audit_error(
            session,
            bot_run_id=bot_run.id,
            correlation_id=cid,
            message="oops",
            details={"cycle": 42},
        )
        assert rec.details["cycle"] == 42
        assert rec.details["_meta"]["correlation_id"] == cid

    def test_recovered_flag(self, session, bot_run):
        rec = audit_error(
            session,
            bot_run_id=bot_run.id,
            correlation_id=_cid(),
            message="recoverable",
            recovered=True,
        )
        assert rec.recovered is True

    def test_no_exception_leaves_traceback_none(self, session, bot_run):
        rec = audit_error(
            session,
            bot_run_id=bot_run.id,
            correlation_id=_cid(),
            message="no exc",
        )
        assert rec.traceback is None
        assert rec.error_type is None


# ---------------------------------------------------------------------------
# ContextVar fallback en helpers
# ---------------------------------------------------------------------------


class TestContextVarFallback:
    """Los helpers leen correlation_id del ContextVar si no se pasa explícitamente."""

    def test_decision_reads_from_contextvar(self, session, bot_run):
        with audit_context() as cid:
            dec = audit_decision(session, bot_run_id=bot_run.id, symbol="BTC-USDT", action="OPEN")
        assert dec.raw_decision["_meta"]["correlation_id"] == cid

    def test_decision_explicit_overrides_contextvar(self, session, bot_run):
        explicit = _cid()
        with audit_context():
            dec = audit_decision(
                session,
                bot_run_id=bot_run.id,
                symbol="BTC-USDT",
                action="OPEN",
                correlation_id=explicit,
            )
        assert dec.raw_decision["_meta"]["correlation_id"] == explicit

    def test_decision_generates_uuid_outside_context(self, session, bot_run):
        dec = audit_decision(session, bot_run_id=bot_run.id, symbol="BTC-USDT", action="OPEN")
        assert len(dec.raw_decision["_meta"]["correlation_id"]) == 36

    def test_snapshot_reads_from_contextvar(self, session, bot_run):
        with audit_context() as cid:
            snap = audit_snapshot(
                session,
                bot_run_id=bot_run.id,
                symbol="BTC-USDT",
                timestamp=datetime.now(UTC),
                open_price=Decimal("60000"),
                high=Decimal("61000"),
                low=Decimal("59000"),
                close=Decimal("60500"),
                volume=Decimal("1500"),
            )
        assert snap.extra["_meta"]["correlation_id"] == cid

    def test_error_reads_from_contextvar(self, session, bot_run):
        with audit_context() as cid:
            rec = audit_error(session, bot_run_id=bot_run.id, message="test")
        assert rec.details["_meta"]["correlation_id"] == cid
