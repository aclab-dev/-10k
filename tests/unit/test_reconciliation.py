"""Tests unitarios — ReconciliationEngine."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderType,
)
from backend.paper.reconciliation import (
    DiscrepancyType,
    ReconciliationEngine,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> PaperAdapter:
    return PaperAdapter(initial_balance_usdt=Decimal("1000"))


def _make_engine(
    adapter: PaperAdapter,
    db_positions: list | None = None,
    db_pending: list | None = None,
    db_filled: list | None = None,
    db_cancelled: list | None = None,
) -> ReconciliationEngine:
    position_repo = MagicMock()
    position_repo.list_open.return_value = db_positions or []

    order_repo = MagicMock()
    order_repo.list_by_status.side_effect = lambda bot_run_id, status, **_: {
        "PENDING": db_pending or [],
        "FILLED": db_filled or [],
        "CANCELLED": db_cancelled or [],
    }.get(status, [])

    return ReconciliationEngine(adapter, position_repo, order_repo)


def _market_buy(
    symbol: str = "BTCUSDT",
    quantity: Decimal = Decimal("0.01"),
    price: Decimal = Decimal("50000"),
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
    )


def _db_position(
    symbol: str,
    quantity: Decimal,
    entry_price: Decimal,
    direction: str = "BUY",
    status: str = "OPEN",
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.quantity = quantity
    pos.entry_price = entry_price
    pos.direction = direction
    pos.status = status
    return pos


def _db_order(client_order_id: str, symbol: str, status: str) -> MagicMock:
    order = MagicMock()
    order.client_order_id = client_order_id
    order.symbol = symbol
    order.status = status
    return order


BOT_RUN_ID = "00000000-0000-0000-0000-000000000001"

# ---------------------------------------------------------------------------
# Estado consistente
# ---------------------------------------------------------------------------


def test_consistent_no_positions_no_orders(adapter: PaperAdapter) -> None:
    engine = _make_engine(adapter)
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent
    assert report.total_discrepancies == 0


def test_consistent_position_matches_db(adapter: PaperAdapter) -> None:
    req = _market_buy(price=Decimal("50000"), quantity=Decimal("0.01"))
    adapter.place_order(req)

    pos = adapter._positions["BTCUSDT"]
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    # Orden también en DB con status correcto
    db_ord = _db_order(req.client_order_id, "BTCUSDT", "FILLED")

    engine = _make_engine(adapter, db_positions=[db_pos], db_filled=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


def test_consistent_pending_order_matches_db(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)

    db_ord = _db_order(req.client_order_id, "BTCUSDT", "PENDING")
    engine = _make_engine(adapter, db_pending=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


# ---------------------------------------------------------------------------
# Discrepancias de posiciones
# ---------------------------------------------------------------------------


def test_position_missing_in_db(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))

    engine = _make_engine(adapter, db_positions=[])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    assert len(report.position_discrepancies) == 1
    disc = report.position_discrepancies[0]
    assert disc.discrepancy_type == DiscrepancyType.MISSING_IN_DB
    assert disc.symbol == "BTCUSDT"
    assert disc.adapter_side == "BUY"


def test_position_missing_in_adapter(adapter: PaperAdapter) -> None:
    db_pos = _db_position("ETHUSDT", Decimal("1"), Decimal("3000"))
    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    assert len(report.position_discrepancies) == 1
    disc = report.position_discrepancies[0]
    assert disc.discrepancy_type == DiscrepancyType.MISSING_IN_ADAPTER
    assert disc.symbol == "ETHUSDT"
    assert disc.db_side == "BUY"


def test_position_quantity_mismatch(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(quantity=Decimal("0.01"), price=Decimal("50000")))

    pos = adapter._positions["BTCUSDT"]
    # DB tiene el doble de la cantidad
    db_pos = _db_position("BTCUSDT", pos.quantity * 2, pos.entry_price)

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    )
    assert disc.symbol == "BTCUSDT"
    assert disc.adapter_quantity == pos.quantity
    assert disc.db_quantity == pos.quantity * 2


def test_position_price_mismatch(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))

    pos = adapter._positions["BTCUSDT"]
    db_pos = _db_position("BTCUSDT", pos.quantity, Decimal("45000"))

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.PRICE_MISMATCH
    )
    assert disc.symbol == "BTCUSDT"
    assert disc.db_entry_price == Decimal("45000")


def test_position_multiple_discrepancies_same_symbol(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(quantity=Decimal("0.01"), price=Decimal("50000")))

    pos = adapter._positions["BTCUSDT"]
    # Both quantity AND price are wrong
    db_pos = _db_position("BTCUSDT", pos.quantity * 2, Decimal("45000"))

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    types = {d.discrepancy_type for d in report.position_discrepancies}
    assert DiscrepancyType.QUANTITY_MISMATCH in types
    assert DiscrepancyType.PRICE_MISMATCH in types


def test_multiple_symbols_independent(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy("BTCUSDT", price=Decimal("50000")))
    adapter.place_order(_market_buy("ETHUSDT", price=Decimal("3000")))

    btc_pos = adapter._positions["BTCUSDT"]
    db_btc = _db_position("BTCUSDT", btc_pos.quantity, btc_pos.entry_price)
    # ETH is missing in DB
    engine = _make_engine(adapter, db_positions=[db_btc])
    report = engine.reconcile(BOT_RUN_ID)

    assert len(report.position_discrepancies) == 1
    assert report.position_discrepancies[0].symbol == "ETHUSDT"
    assert report.position_discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_IN_DB


# ---------------------------------------------------------------------------
# Discrepancias de órdenes
# ---------------------------------------------------------------------------


def test_order_missing_in_db(adapter: PaperAdapter) -> None:
    req = _market_buy(price=Decimal("50000"))
    adapter.place_order(req)

    engine = _make_engine(adapter, db_positions=[], db_filled=[], db_pending=[], db_cancelled=[])
    report = engine.reconcile(BOT_RUN_ID)

    order_discs = [
        d for d in report.order_discrepancies if d.discrepancy_type == DiscrepancyType.MISSING_IN_DB
    ]
    assert len(order_discs) == 1
    assert order_discs[0].client_order_id == req.client_order_id
    assert order_discs[0].adapter_status == "FILLED"


def test_order_missing_in_adapter(adapter: PaperAdapter) -> None:
    import uuid

    ghost_coid = str(uuid.uuid4())
    db_ord = _db_order(ghost_coid, "BTCUSDT", "PENDING")

    engine = _make_engine(adapter, db_pending=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)

    discs = [
        d
        for d in report.order_discrepancies
        if d.discrepancy_type == DiscrepancyType.MISSING_IN_ADAPTER
    ]
    assert len(discs) == 1
    assert discs[0].client_order_id == ghost_coid
    assert discs[0].db_status == "PENDING"


def test_order_status_mismatch_filled_vs_pending(adapter: PaperAdapter) -> None:
    req = _market_buy(price=Decimal("50000"))
    adapter.place_order(req)  # status=FILLED in adapter

    # DB still shows PENDING
    db_ord = _db_order(req.client_order_id, "BTCUSDT", "PENDING")
    engine = _make_engine(adapter, db_pending=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)

    discs = [
        d
        for d in report.order_discrepancies
        if d.discrepancy_type == DiscrepancyType.STATUS_MISMATCH
    ]
    assert len(discs) == 1
    assert discs[0].adapter_status == "FILLED"
    assert discs[0].db_status == "PENDING"


def test_order_status_mismatch_cancelled_vs_pending(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)
    adapter.cancel_order(req.client_order_id)  # status=CANCELLED in adapter

    db_ord = _db_order(req.client_order_id, "BTCUSDT", "PENDING")
    engine = _make_engine(adapter, db_pending=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)

    discs = [
        d
        for d in report.order_discrepancies
        if d.discrepancy_type == DiscrepancyType.STATUS_MISMATCH
    ]
    assert len(discs) == 1
    assert discs[0].adapter_status == "CANCELLED"
    assert discs[0].db_status == "PENDING"


def test_order_consistent_cancelled_matches_db(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)
    adapter.cancel_order(req.client_order_id)

    db_ord = _db_order(req.client_order_id, "BTCUSDT", "CANCELLED")
    engine = _make_engine(adapter, db_cancelled=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


# ---------------------------------------------------------------------------
# ReconciliationReport helpers
# ---------------------------------------------------------------------------


def test_report_is_consistent_property_with_discrepancies(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    engine = _make_engine(adapter, db_positions=[])  # MISSING_IN_DB
    report = engine.reconcile(BOT_RUN_ID)
    assert not report.is_consistent
    assert report.total_discrepancies >= 1


def test_report_total_discrepancies_sums_both(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    # Position missing AND order missing → 2 discrepancies
    engine = _make_engine(adapter, db_positions=[], db_filled=[], db_pending=[], db_cancelled=[])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.total_discrepancies == 2


def test_price_tolerance_prevents_false_positive(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter._positions["BTCUSDT"]

    # Price differs by less than default tolerance — should not flag
    tiny_diff = Decimal("0.000000001")
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price + tiny_diff)

    req = next(iter(adapter._orders.values()))
    db_ord = _db_order(req.client_order_id, "BTCUSDT", req.status.value)
    engine = _make_engine(adapter, db_positions=[db_pos], db_filled=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent
