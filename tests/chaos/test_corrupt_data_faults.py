"""Caos — datos corruptos (F16 [118]).

Inyecta payloads inválidos (respuesta GPT fuera de schema, snapshot stale /
con clock skew, estado de exchange que contradice la DB) y valida que el bot
los rechaza o los acciona en vez de operar sobre ellos.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.connection_health.schemas import ConnectionAnomalyReason
from backend.core.config import get_config
from backend.decision_engine.gpt_client import (
    GPTClient,
    GPTRequest,
    GPTResponseValidationError,
    RequestPurpose,
    _RawCallResult,
)
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.market_data.fetcher import MockDataFetcher
from backend.market_data.validators import SnapshotRejectedError, validate_snapshot
from backend.position_manager.manager import PositionManager
from backend.reconciliation.engine import DiscrepancyType, ReconciliationEngine
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import SystemEvent
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from tests.chaos.faults import ChaosAdapter, ChaosFetcher
from tests.unit.conftest import make_bot_run, make_bot_state

pytestmark = pytest.mark.chaos

_BOT_RUN_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# GPT — respuesta fuera de schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_gpt_response_always_blocks_regardless_of_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una respuesta de GPT que no matchea ModelDecision NO es "GPT no
    disponible": bloquea siempre, incluso con gpt_failure_blocks_new_entries
    desactivado."""
    policy = get_config().failure_policy.model_copy(
        update={"gpt_failure_blocks_new_entries": False}
    )
    client = GPTClient(api_key="test-key", failure_policy=policy)
    monkeypatch.setattr(
        client,
        "_call_with_retry",
        AsyncMock(
            return_value=_RawCallResult(
                content='{"totally": "not a decision"}',
                raw_json='{"totally": "not a decision"}',
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                finish_reason="stop",
            )
        ),
    )

    req = GPTRequest(system_prompt="s", user_prompt="u", prompt_version="v1")
    with pytest.raises(GPTResponseValidationError):
        await client.request(req, RequestPurpose.NEW_ENTRY)

    await client.aclose()


# ---------------------------------------------------------------------------
# Market data — snapshot corrupto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_snapshot_is_rejected_by_the_guard() -> None:
    """El ChaosFetcher envejece el timestamp del snapshot → el Market Data Guard
    lo rechaza y nunca entra al pipeline."""
    fetcher = ChaosFetcher(MockDataFetcher(seed=1))
    fetcher.mutate_symbol("BTCUSDT", timestamp_utc=datetime.now(UTC) - timedelta(seconds=45))
    snapshot = await fetcher.fetch_snapshot("BTCUSDT", Decimal("1000"))

    with pytest.raises(SnapshotRejectedError):
        validate_snapshot(snapshot)


@pytest.mark.asyncio
async def test_clock_skew_snapshot_forces_safe_mode(session: Session) -> None:
    """El snapshot llega (dentro del bound duro) pero con clock_skew sobre el
    umbral de alerta temprana → SAFE_MODE."""
    bot_run = make_bot_run(session, status="RUNNING")
    fetcher = ChaosFetcher(MockDataFetcher(seed=2))
    fetcher.mutate_symbol("BTCUSDT", clock_skew_ms=3500)
    btc = await fetcher.fetch_snapshot("BTCUSDT", Decimal("1000"))
    eth = await MockDataFetcher(seed=3).fetch_snapshot("ETHUSDT", Decimal("1000"))

    sm = BotStateMachine(initial=BotState.ACTIVE)
    monitor = ConnectionHealthMonitor(
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        max_clock_skew_ms=2000,
        max_latency_ms=3000,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )

    findings = monitor.check_and_enforce([btc, eth])

    assert any(
        f.reason == ConnectionAnomalyReason.CLOCK_SKEW_EXCEEDED and f.symbol == "BTCUSDT"
        for f in findings
    )
    assert sm.state == BotState.SAFE_MODE


# ---------------------------------------------------------------------------
# Reconciliación — exchange contradice la DB
# ---------------------------------------------------------------------------


def _reco_engine(
    adapter: PaperAdapter | ChaosAdapter,
    *,
    db_positions: list[object] | None = None,
    db_pending: list[object] | None = None,
    db_known: list[object] | None = None,
    position_manager: PositionManager | None = None,
) -> ReconciliationEngine:
    position_repo = MagicMock()
    position_repo.list_open.return_value = db_positions or []
    order_repo = MagicMock()
    order_repo.list_by_status.side_effect = lambda _run, status, **_: (
        list(db_pending or []) if status == "PENDING" else []
    )
    known = list(db_pending or []) + list(db_known or [])
    order_repo.list_by_client_order_ids.side_effect = lambda coids: [
        o for o in known if o.client_order_id in set(coids)
    ]
    return ReconciliationEngine(
        adapter,
        position_repo,
        order_repo,
        position_manager=position_manager,
        symbols=frozenset({"BTCUSDT"}),
    )


def _db_row(**attrs: object) -> MagicMock:
    row = MagicMock()
    for key, value in attrs.items():
        setattr(row, key, value)
    return row


def test_reconciliation_flags_status_mismatch_when_exchange_still_shows_order_open() -> None:
    """La DB da la orden por FILLED pero el exchange la sigue reportando viva:
    el caso peligroso — se marca STATUS_MISMATCH, el reporte no es consistente."""
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter.set_leverage("BTCUSDT", 1)
    pending = adapter.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=Decimal("10000"),
        )
    )
    db_order = _db_row(client_order_id=pending.client_order_id, symbol="BTCUSDT", status="FILLED")
    engine = _reco_engine(adapter, db_known=[db_order])

    report = engine.reconcile(_BOT_RUN_ID)

    assert [d.discrepancy_type for d in report.order_discrepancies] == [
        DiscrepancyType.STATUS_MISMATCH
    ]
    assert report.is_consistent is False


def test_reconciliation_flags_quantity_mismatch_between_exchange_and_db() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.02"),
            price=Decimal("50000"),
        )
    )
    adapter_pos = adapter.get_position("BTCUSDT")
    assert adapter_pos is not None
    db_pos = _db_row(
        symbol="BTCUSDT",
        quantity=Decimal("0.01"),  # la mitad de lo que reporta el exchange
        entry_price=adapter_pos.entry_price,
        direction="BUY",
        status="OPEN",
    )
    engine = _reco_engine(adapter, db_positions=[db_pos])

    report = engine.reconcile(_BOT_RUN_ID)

    assert DiscrepancyType.QUANTITY_MISMATCH in {
        d.discrepancy_type for d in report.position_discrepancies
    }
    assert report.is_consistent is False


def test_reconciliation_flags_missing_protection_for_unwatched_position() -> None:
    adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            price=Decimal("50000"),
        )
    )
    adapter_pos = adapter.get_position("BTCUSDT")
    assert adapter_pos is not None
    db_pos = _db_row(
        symbol="BTCUSDT",
        quantity=adapter_pos.quantity,
        entry_price=adapter_pos.entry_price,
        direction="BUY",
        status="OPEN",
    )
    # PositionManager sin ningún set_config → la posición no está vigilada.
    engine = _reco_engine(adapter, db_positions=[db_pos], position_manager=PositionManager(adapter))

    report = engine.reconcile(_BOT_RUN_ID)

    assert DiscrepancyType.MISSING_PROTECTION in {
        d.discrepancy_type for d in report.position_discrepancies
    }


def test_reconciliation_flags_position_vanished_from_exchange() -> None:
    """El exchange deja de reportar una posición que la DB tiene OPEN (el feed
    de posiciones miente por omisión): se marca MISSING_IN_ADAPTER y el reporte
    no es consistente — nunca se asume que la posición se cerró sola."""
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    inner.set_leverage("BTCUSDT", 1)
    inner.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            price=Decimal("50000"),
        )
    )
    adapter_pos = inner.get_position("BTCUSDT")
    assert adapter_pos is not None
    adapter = ChaosAdapter(inner)
    adapter.vanish_position("BTCUSDT")  # el exchange lo ve flat

    db_pos = _db_row(
        symbol="BTCUSDT",
        quantity=adapter_pos.quantity,
        entry_price=adapter_pos.entry_price,
        direction="BUY",
        status="OPEN",
    )
    engine = _reco_engine(adapter, db_positions=[db_pos])

    report = engine.reconcile(_BOT_RUN_ID)

    assert DiscrepancyType.MISSING_IN_ADAPTER in {
        d.discrepancy_type for d in report.position_discrepancies
    }
    assert report.is_consistent is False
    assert adapter.call_count["get_position"] >= 1  # la lectura instrumentada corrió


# ---------------------------------------------------------------------------
# Estado persistido corrupto
# ---------------------------------------------------------------------------


def test_corrupt_persisted_bot_state_does_not_trigger_bogus_transition(session: Session) -> None:
    """bot_state.state trae un valor fuera del enum: el monitor no inventa una
    transición ni deja el lock FOR UPDATE abierto — loguea y no hace nada."""
    bot_run = make_bot_run(session, status="RUNNING")
    make_bot_state(session, bot_run, state="GARBAGE_STATE", previous_state="ACTIVE")
    session.commit()

    sm = BotStateMachine(initial=BotState.ACTIVE)
    monitor = ConnectionHealthMonitor(
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        max_clock_skew_ms=2000,
        max_latency_ms=3000,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )

    findings = monitor.check_and_enforce([])  # hay hallazgos, pero el estado no se puede resolver

    # El lock FOR UPDATE se soltó (rollback) antes de que nada más toque la sesión.
    assert session.in_transaction() is False
    assert len(findings) == 2
    assert sm.state == BotState.ACTIVE
    rows = session.query(BotStateRow).filter_by(bot_run_id=bot_run.id).all()
    assert len(rows) == 1  # solo la fila corrupta preexistente
    assert session.query(SystemEvent).count() == 0
