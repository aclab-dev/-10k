"""Tests unitarios — ReconciliationEngine."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.position_manager.manager import PositionManager
from backend.position_manager.schemas import PositionConfig
from backend.reconciliation.engine import (
    _ORDER_FETCH_LIMIT,
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
    db_known: list | None = None,
    position_manager: PositionManager | None = None,
    symbols: frozenset[str] | None = None,
    decimal_tolerance: Decimal | None = None,
) -> ReconciliationEngine:
    position_repo = MagicMock()
    position_repo.list_open.return_value = db_positions or []

    db_pending = db_pending or []
    order_repo = MagicMock()
    order_repo.list_by_status.side_effect = lambda bot_run_id, status, **_: (
        list(db_pending) if status == "PENDING" else []
    )
    # Filas completas por client_order_id: los tests pasan sus filas "conocidas"
    # vía db_pending (y db_known para status no-PENDING).
    known_rows = list(db_pending) + list(db_known or [])
    order_repo.list_by_client_order_ids.side_effect = lambda coids: [
        o for o in known_rows if o.client_order_id in set(coids)
    ]

    kwargs = {}
    if decimal_tolerance is not None:
        kwargs["decimal_tolerance"] = decimal_tolerance
    return ReconciliationEngine(
        adapter,
        position_repo,
        order_repo,
        position_manager=position_manager,
        symbols=symbols,
        **kwargs,
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


def test_manual_tp_change_detected_when_adapter_reports_it(adapter: PaperAdapter) -> None:
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)
    position_manager.set_config(PositionConfig(symbol="BTCUSDT", take_profit=Decimal("55000")))

    adapter.get_position = MagicMock(  # type: ignore[method-assign]
        return_value=pos.model_copy(update={"take_profit": Decimal("60000")})
    )

    engine = _make_engine(
        adapter,
        db_positions=[db_pos],
        position_manager=position_manager,
        symbols=frozenset({"BTCUSDT"}),
    )
    report = engine.reconcile(BOT_RUN_ID)

    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.MANUAL_SL_TP_CHANGE
    )
    assert disc.adapter_take_profit == Decimal("60000")
    assert disc.config_take_profit == Decimal("55000")


def test_manual_sl_change_compares_against_effective_sl_not_static_config(
    adapter: PaperAdapter,
) -> None:
    """Tras update_sl(), el SL efectivo diverge de config.stop_loss de forma legítima:
    si el exchange reporta el SL efectivo, NO debe marcarse como cambio manual."""
    adapter.place_order(_market_buy(price=Decimal("50000")))
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    db_pos = _db_position("BTCUSDT", pos.quantity, pos.entry_price)

    position_manager = PositionManager(adapter)
    position_manager.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))
    position_manager.update_sl("BTCUSDT", Decimal("49000"))  # SL efectivo ahora 49000

    # El adapter real reporta el SL efectivo (49000), no el estático de config (48000).
    adapter.get_position = MagicMock(  # type: ignore[method-assign]
        return_value=pos.model_copy(update={"stop_loss": Decimal("49000")})
    )

    engine = _make_engine(
        adapter,
        db_positions=[db_pos],
        position_manager=position_manager,
        symbols=frozenset({"BTCUSDT"}),
    )
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


def test_status_mismatch_order_alive_on_exchange_but_resolved_locally(
    adapter: PaperAdapter,
) -> None:
    """La orden sigue viva en el exchange (PENDING) pero localmente quedó FAILED
    (ej. timeout de confirmación). Debe levantar STATUS_MISMATCH, no pasar en silencio."""
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("48000"),
    )
    adapter.place_order(req)  # PENDING en el adapter, visible vía get_open_orders

    # Fila local conocida pero con status resuelto (no PENDING) → va en db_known.
    db_ord = _db_order(req.client_order_id, "BTCUSDT", "FAILED")
    engine = _make_engine(adapter, db_pending=[], db_known=[db_ord])
    report = engine.reconcile(BOT_RUN_ID)

    disc = next(
        d
        for d in report.order_discrepancies
        if d.discrepancy_type == DiscrepancyType.STATUS_MISMATCH
    )
    assert disc.client_order_id == req.client_order_id
    assert disc.adapter_status == "PENDING"
    assert disc.db_status == "FAILED"


def test_order_fetch_limit_warning_emitted(adapter: PaperAdapter) -> None:
    """Si list_by_status devuelve exactamente el límite, se emite un warning estructurado."""
    limit_rows = [
        _db_order(str(uuid.uuid4()), "BTCUSDT", "PENDING") for _ in range(_ORDER_FETCH_LIMIT)
    ]

    position_repo = MagicMock()
    position_repo.list_open.return_value = []

    order_repo = MagicMock()
    order_repo.list_by_status.side_effect = lambda bot_run_id, status, **_: (
        list(limit_rows) if status == "PENDING" else []
    )
    order_repo.list_by_client_order_ids.side_effect = lambda coids: []

    engine = ReconciliationEngine(adapter, position_repo, order_repo)

    with patch("backend.reconciliation.engine._log") as mock_log:
        engine.reconcile(BOT_RUN_ID)

    mock_log.warning.assert_called_once()
    assert mock_log.warning.call_args[0][0] == "reconciliation.order_fetch_limit_reached"


# ---------------------------------------------------------------------------
# Símbolos fuera de la lista configurada (symbols=)
# ---------------------------------------------------------------------------


def test_db_position_on_unconfigured_symbol_is_still_reconciled(adapter: PaperAdapter) -> None:
    """Una posición OPEN en DB cuyo símbolo no está en symbols= igual debe chequearse."""
    db_pos = _db_position("ETHUSDT", Decimal("1"), Decimal("3000"))
    engine = _make_engine(adapter, db_positions=[db_pos], symbols=frozenset({"BTCUSDT"}))
    report = engine.reconcile(BOT_RUN_ID)

    disc = next(
        d
        for d in report.position_discrepancies
        if d.discrepancy_type == DiscrepancyType.MISSING_IN_ADAPTER
    )
    assert disc.symbol == "ETHUSDT"


def test_db_pending_order_on_unconfigured_symbol_is_not_false_flagged(
    adapter: PaperAdapter,
) -> None:
    """Una orden PENDING en DB de un símbolo fuera de symbols= no debe marcarse
    MISSING_IN_ADAPTER si en realidad sigue viva en el exchange."""
    req = OrderRequest(
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.1"),
        price=Decimal("2800"),
    )
    adapter.place_order(req)
    db_ord = _db_order(req.client_order_id, "ETHUSDT", "PENDING")

    engine = _make_engine(adapter, db_pending=[db_ord], symbols=frozenset({"BTCUSDT"}))
    report = engine.reconcile(BOT_RUN_ID)
    assert report.is_consistent


# ---------------------------------------------------------------------------
# Reporte parcial (fallas de fetch por símbolo)
# ---------------------------------------------------------------------------


def test_report_incomplete_and_not_consistent_when_all_fetches_fail(
    adapter: PaperAdapter,
) -> None:
    boom = RuntimeError("exchange API down")
    adapter.get_position = MagicMock(side_effect=boom)  # type: ignore[method-assign]
    adapter.get_open_orders = MagicMock(side_effect=boom)  # type: ignore[method-assign]

    engine = _make_engine(adapter)
    report = engine.reconcile(BOT_RUN_ID)

    assert not report.is_complete
    assert not report.is_consistent  # parcial → no se puede afirmar consistencia
    assert set(report.failed_symbols) == set(ALLOWED_SYMBOLS)
    assert report.total_discrepancies == 0


def test_failed_symbol_does_not_produce_false_missing_in_adapter(adapter: PaperAdapter) -> None:
    """Si get_open_orders falla para un símbolo, sus PENDING locales no deben marcarse
    MISSING_IN_ADAPTER: no se pudo consultar al exchange."""
    ghost = _db_order(str(uuid.uuid4()), "BTCUSDT", "PENDING")
    adapter.get_open_orders = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("timeout")
    )

    engine = _make_engine(adapter, db_pending=[ghost])
    report = engine.reconcile(BOT_RUN_ID)

    assert "BTCUSDT" in report.failed_symbols
    assert not any(
        d.discrepancy_type == DiscrepancyType.MISSING_IN_ADAPTER for d in report.order_discrepancies
    )


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
