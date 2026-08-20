"""Tests unitarios — ReconciliationEngine."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from backend.position_manager.manager import PositionManager
from backend.position_manager.schemas import PositionConfig
from backend.reconciliation.engine import (
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
    position_manager: PositionManager | None = None,
    decimal_tolerance: Decimal | None = None,
) -> ReconciliationEngine:
    position_repo = MagicMock()
    position_repo.list_open.return_value = db_positions or []

    db_pending = db_pending or []
    order_repo = MagicMock()
    order_repo.list_by_status.side_effect = lambda bot_run_id, status, **_: (
        db_pending if status == "PENDING" else []
    )
    known_ids_by_coid = {o.client_order_id for o in db_pending}
    order_repo.list_known_client_order_ids.side_effect = lambda coids: {
        c for c in coids if c in known_ids_by_coid
    }

    kwargs = {}
    if decimal_tolerance is not None:
        kwargs["decimal_tolerance"] = decimal_tolerance
    return ReconciliationEngine(
        adapter, position_repo, order_repo, position_manager=position_manager, **kwargs
    )


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

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    engine = _make_engine(adapter, db_positions=[db_pos])
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

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
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

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
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


def test_position_side_mismatch(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    # DB registra SELL, adapter tiene BUY
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price, direction="SELL")

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.SIDE_MISMATCH
    )
    assert disc.adapter_side == "BUY"
    assert disc.db_side == "SELL"


def test_position_multiple_discrepancies_same_symbol(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(quantity=Decimal("0.01"), price=Decimal("50000")))

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity * 2, Decimal("45000"))

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)

    types = {d.discrepancy_type for d in report.position_discrepancies}
    assert DiscrepancyType.QUANTITY_MISMATCH in types
    assert DiscrepancyType.PRICE_MISMATCH in types


def test_multiple_symbols_independent(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy("BTCUSDT", price=Decimal("50000")))
    adapter.place_order(_market_buy("ETHUSDT", price=Decimal("3000")))

    btc_pos = adapter.get_position("BTCUSDT")
    assert btc_pos is not None
    db_btc = _db_position("BTCUSDT", btc_pos.quantity, btc_pos.entry_price)
    # ETH is missing in DB
    engine = _make_engine(adapter, db_positions=[db_btc])
    report = engine.reconcile(BOT_RUN_ID)

    assert len(report.position_discrepancies) == 1
    assert report.position_discrepancies[0].symbol == "ETHUSDT"
    assert report.position_discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_IN_DB


# ---------------------------------------------------------------------------
# Protecciones (PositionManager)
# ---------------------------------------------------------------------------


def test_missing_protection_without_position_manager_is_noop(adapter: PaperAdapter) -> None:
    """Sin PositionManager inyectado, no se chequea protección (compatibilidad)."""
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


def test_missing_protection_detected(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)  # sin config para BTCUSDT
    engine = _make_engine(adapter, db_positions=[db_pos], position_manager=position_manager)
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.MISSING_PROTECTION
    )
    assert disc.symbol == "BTCUSDT"


def test_protection_present_no_discrepancy(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)
    position_manager.set_config(
        PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000"), take_profit=Decimal("55000"))
    )
    engine = _make_engine(adapter, db_positions=[db_pos], position_manager=position_manager)
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


def test_manual_sl_change_detected_when_adapter_reports_it(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)
    position_manager.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

    # PaperAdapter nunca reporta stop_loss propio: simulamos un adapter real que sí lo hace.
    adapter.get_position = MagicMock(  # type: ignore[method-assign]
        return_value=pos.model_copy(update={"stop_loss": Decimal("47000")})
    )

    engine = _make_engine(adapter, db_positions=[db_pos], position_manager=position_manager)
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.MANUAL_SL_TP_CHANGE
    )
    assert disc.symbol == "BTCUSDT"
    assert disc.adapter_stop_loss == Decimal("47000")
    assert disc.config_stop_loss == Decimal("48000")


def test_manual_sl_change_not_flagged_when_adapter_silent(adapter: PaperAdapter) -> None:
    """PaperAdapter no reporta SL propio: no debe compararse contra PositionConfig."""
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)
    position_manager.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

    engine = _make_engine(adapter, db_positions=[db_pos], position_manager=position_manager)
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


# ---------------------------------------------------------------------------
# Discrepancias de órdenes
# ---------------------------------------------------------------------------


def test_order_missing_in_db(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)  # queda PENDING (LIMIT) y visible via get_open_orders

    engine = _make_engine(adapter, db_positions=[], db_pending=[])
    report = engine.reconcile(BOT_RUN_ID)

    order_discs = [
        d for d in report.order_discrepancies if d.discrepancy_type == DiscrepancyType.MISSING_IN_DB
    ]
    assert len(order_discs) == 1
    assert order_discs[0].client_order_id == req.client_order_id
    assert order_discs[0].adapter_status == "PENDING"


def test_order_missing_in_adapter(adapter: PaperAdapter) -> None:
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


def test_order_consistent_pending_matches_db(adapter: PaperAdapter) -> None:
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


def test_partial_fill_detected(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)
    db_ord = _db_order(req.client_order_id, "BTCUSDT", "PENDING")

    partial_order = adapter.get_open_orders("BTCUSDT")[0].model_copy(
        update={"status": OrderStatus.PARTIALLY_FILLED, "quantity_filled": Decimal("0.004")}
    )
    adapter.get_open_orders = MagicMock(return_value=[partial_order])  # type: ignore[method-assign]

    engine = _make_engine(adapter, db_pending=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_consistent
    disc = next(
        d for d in report.order_discrepancies if d.discrepancy_type == DiscrepancyType.PARTIAL_FILL
    )
    assert disc.client_order_id == req.client_order_id
    assert disc.quantity_filled == Decimal("0.004")
    assert disc.quantity_requested == Decimal("0.01")


# ---------------------------------------------------------------------------
# ReconciliationReport helpers
# ---------------------------------------------------------------------------


def test_report_is_consistent_property_with_discrepancies(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    engine = _make_engine(adapter, db_positions=[])
    report = engine.reconcile(BOT_RUN_ID)
    assert not report.is_consistent
    assert report.total_discrepancies >= 1


def test_report_total_discrepancies_sums_both(adapter: PaperAdapter) -> None:
    adapter.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=Decimal("48000"),
        )
    )
    # Position missing (n/a, LIMIT no abre posicion) AND order missing → 1 discrepancy de orden
    engine = _make_engine(adapter, db_positions=[], db_pending=[])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.total_discrepancies == 1


def test_decimal_tolerance_prevents_false_positive(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None

    # Price differs by less than default tolerance — should not flag
    tiny_diff = Decimal("0.000000001")
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price + tiny_diff)

    engine = _make_engine(adapter, db_positions=[db_pos])
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


def test_custom_decimal_tolerance_constructor(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None

    # With a very loose tolerance, a 1-unit price diff should pass
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price + Decimal("0.5"))

    engine = _make_engine(
        adapter,
        db_positions=[db_pos],
        decimal_tolerance=Decimal("1"),
    )
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent
