"""Tests unitarios del TokenBudgetManager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core.config import FailurePolicyConfig, TokenBudgetConfig
from backend.decision_engine.gpt_client import RequestPurpose
from backend.storage.database import Base
from backend.storage.models import BotRun, TokenUsage
from backend.token_budget.manager import TokenBudgetExceededError, TokenBudgetManager
from backend.token_budget.schemas import BudgetStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CFG = TokenBudgetConfig(
    max_tokens_per_request=4000,
    max_tokens_per_hour=1000,
    max_tokens_per_day=5000,
    alert_at_percent=0.8,
)

_POLICY_BLOCKS = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=True,
    token_budget_failure_blocks_new_entries=True,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)

_POLICY_SOFT = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=False,
    token_budget_failure_blocks_new_entries=False,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)


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


def _bot_run(session: Session) -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


def _insert_usage(
    session: Session,
    bot_run_id: str,
    total_tokens: int,
    *,
    minutes_ago: int = 0,
) -> None:
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    record = TokenUsage(
        bot_run_id=bot_run_id,
        model="gpt-4o",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens - total_tokens // 2,
        total_tokens=total_tokens,
        timestamp=ts,
    )
    session.add(record)
    session.flush()


def _make_manager(
    session: Session,
    bot_run_id: str,
    *,
    policy: FailurePolicyConfig = _POLICY_BLOCKS,
    config: TokenBudgetConfig = _CFG,
) -> TokenBudgetManager:
    return TokenBudgetManager(
        session=session,
        bot_run_id=bot_run_id,
        config=config,
        failure_policy=policy,
    )


# ---------------------------------------------------------------------------
# check_budget — POSITION_MANAGEMENT
# ---------------------------------------------------------------------------


class TestCheckBudgetPositionManagement:
    def test_always_ok_even_when_exceeded(self, session: Session) -> None:
        run = _bot_run(session)
        # Forzar hora excedida
        _insert_usage(session, run.id, 2000, minutes_ago=5)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.POSITION_MANAGEMENT)
        assert result.ok is True
        assert result.status == BudgetStatus.NORMAL

    def test_never_raises_even_when_policy_blocks(self, session: Session) -> None:
        run = _bot_run(session)
        _insert_usage(session, run.id, 9999, minutes_ago=5)
        mgr = _make_manager(session, run.id, policy=_POLICY_BLOCKS)
        # No debe levantar excepción
        result = mgr.check_budget(RequestPurpose.POSITION_MANAGEMENT)
        assert result.ok is True


# ---------------------------------------------------------------------------
# check_budget — NEW_ENTRY normal / warning / exceeded
# ---------------------------------------------------------------------------


class TestCheckBudgetNewEntry:
    def test_normal_when_no_usage(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.ok is True
        assert result.status == BudgetStatus.NORMAL
        assert result.tokens_used_hour == 0
        assert result.tokens_used_day == 0

    def test_warning_when_near_hour_limit(self, session: Session) -> None:
        run = _bot_run(session)
        # 80% del límite por hora (1000 * 0.8 = 800)
        _insert_usage(session, run.id, 850, minutes_ago=10)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.ok is True
        assert result.status == BudgetStatus.WARNING

    def test_warning_when_near_day_limit(self, session: Session) -> None:
        run = _bot_run(session)
        # 80% del límite diario (5000 * 0.8 = 4000)
        _insert_usage(session, run.id, 4100, minutes_ago=120)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.ok is True
        assert result.status == BudgetStatus.WARNING

    def test_exceeded_hour_with_policy_blocks_raises(self, session: Session) -> None:
        run = _bot_run(session)
        _insert_usage(session, run.id, 1200, minutes_ago=5)
        mgr = _make_manager(session, run.id, policy=_POLICY_BLOCKS)
        with pytest.raises(TokenBudgetExceededError):
            mgr.check_budget(RequestPurpose.NEW_ENTRY)

    def test_exceeded_day_with_policy_blocks_raises(self, session: Session) -> None:
        run = _bot_run(session)
        # Dentro de hora < 1000, pero día > 5000
        _insert_usage(session, run.id, 500, minutes_ago=10)
        _insert_usage(session, run.id, 5100, minutes_ago=200)
        mgr = _make_manager(session, run.id, policy=_POLICY_BLOCKS)
        with pytest.raises(TokenBudgetExceededError):
            mgr.check_budget(RequestPurpose.NEW_ENTRY)

    def test_exceeded_with_soft_policy_returns_not_ok(self, session: Session) -> None:
        run = _bot_run(session)
        _insert_usage(session, run.id, 1500, minutes_ago=5)
        mgr = _make_manager(session, run.id, policy=_POLICY_SOFT)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.ok is False
        assert result.status == BudgetStatus.EXCEEDED

    def test_old_usage_outside_window_ignored(self, session: Session) -> None:
        run = _bot_run(session)
        # 90 horas atrás → fuera de ventana de 24h
        _insert_usage(session, run.id, 9999, minutes_ago=90 * 60)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.ok is True
        assert result.status == BudgetStatus.NORMAL

    def test_result_carries_limit_values(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.limit_hour == _CFG.max_tokens_per_hour
        assert result.limit_day == _CFG.max_tokens_per_day


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


class TestRecordUsage:
    def test_persists_token_usage_record(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)
        record = mgr.record_usage(
            prompt_tokens=300,
            completion_tokens=150,
            model="gpt-4o",
        )
        assert record.id is not None
        assert record.bot_run_id == run.id
        assert record.prompt_tokens == 300
        assert record.completion_tokens == 150
        assert record.total_tokens == 450
        assert record.model == "gpt-4o"
        assert record.model_request_id is None

    def test_record_usage_affects_subsequent_check(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)

        # Sin uso → NORMAL
        r1 = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert r1.status == BudgetStatus.NORMAL

        # Registrar 850 tokens → WARNING (>= 80% de 1000)
        mgr.record_usage(prompt_tokens=500, completion_tokens=350, model="gpt-4o")

        r2 = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert r2.status == BudgetStatus.WARNING
        assert r2.tokens_used_hour == 850

    def test_record_usage_with_model_request_id(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)
        # model_request_id FK puede quedar NULL en tests (SET NULL on delete)
        record = mgr.record_usage(
            prompt_tokens=100,
            completion_tokens=50,
            model="gpt-4o",
            model_request_id=None,
        )
        assert record.model_request_id is None

    def test_record_usage_accumulates(self, session: Session) -> None:
        run = _bot_run(session)
        mgr = _make_manager(session, run.id)
        mgr.record_usage(prompt_tokens=200, completion_tokens=100, model="gpt-4o")
        mgr.record_usage(prompt_tokens=200, completion_tokens=100, model="gpt-4o")
        result = mgr.check_budget(RequestPurpose.NEW_ENTRY)
        assert result.tokens_used_hour == 600
