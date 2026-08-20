"""ReconciliationEngine — compara estado local (DB) vs exchange (F16 [116]).

Recorre símbolo por símbolo usando la `ExchangeAdapter` genérica (`get_position` /
`get_open_orders`) — el mismo contrato que usa `OrphanOrderScanner` (F16 [115]) —
así que funciona igual contra `PaperAdapter` que contra un adapter real
(BingX/Binance). Compara ese estado contra lo persistido en DB y produce un
`ReconciliationReport` con todas las discrepancias encontradas.

Scope:
- Solo detecta. No corrige ni muta el adapter ni la DB. La respuesta a los
  hallazgos (ej. disparar SAFE_MODE) es responsabilidad de otros componentes
  (ver OrphanOrderScanner para el caso de posiciones sin protección y órdenes
  huérfanas).
- Posiciones: se comparan todas las posiciones OPEN en DB contra
  `adapter.get_position(symbol)` para cada símbolo configurado.
- Órdenes: `get_open_orders(symbol)` solo devuelve órdenes vivas (PENDING) —
  a diferencia de un `PaperAdapter` standalone, un exchange real no expone
  historial de órdenes ya resueltas (FILLED/CANCELLED) por esta vía. Por eso
  el scope de órdenes se limita a: huérfanas en el exchange (sin fila local) y
  huérfanas en DB (PENDING localmente pero ya no vivas en el exchange — se
  resolvieron fuera del bot). Fills parciales se detectan comparando
  `quantity_filled` contra `quantity_requested` en las que sí aparecen en
  ambos lados.
- Protecciones: si se inyecta un `PositionManager`, se valida que toda
  posición abierta tenga un `PositionConfig` activo vigilándola (mismo
  criterio que `UNPROTECTED_POSITION` en OrphanOrderScanner) y que, cuando el
  adapter reporta un stop_loss/take_profit propio (algunos exchanges reales
  lo hacen; PaperAdapter nunca), coincida con el configurado localmente — una
  discrepancia ahí implica que alguien lo cambió manualmente en el exchange.
- Discrepancias numéricas se evalúan con una tolerancia configurable para
  evitar falsos positivos por redondeo (aplica a precios, cantidades y SL/TP).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

import structlog
from pydantic import BaseModel, Field

from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import OrderResult, OrderStatus, PositionState
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.position_manager.manager import PositionManager
from backend.storage.models import Order as DbOrder
from backend.storage.models import Position as DbPosition
from backend.storage.repositories.trades import OrderRepository, PositionRepository

_log = structlog.get_logger(__name__)

# Tolerancia por defecto para comparar precios, cantidades y SL/TP.
_DEFAULT_DECIMAL_TOLERANCE = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Enums y schemas del reporte
# ---------------------------------------------------------------------------


class DiscrepancyType(StrEnum):
    MISSING_IN_DB = "MISSING_IN_DB"
    MISSING_IN_ADAPTER = "MISSING_IN_ADAPTER"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    SIDE_MISMATCH = "SIDE_MISMATCH"
    PARTIAL_FILL = "PARTIAL_FILL"
    MISSING_PROTECTION = "MISSING_PROTECTION"
    MANUAL_SL_TP_CHANGE = "MANUAL_SL_TP_CHANGE"


class PositionDiscrepancy(BaseModel):
    """Discrepancia detectada en el estado de una posición."""

    symbol: str
    discrepancy_type: DiscrepancyType
    detail: str
    # Estado en el adapter (None si no existe ahí)
    adapter_quantity: Decimal | None = None
    adapter_entry_price: Decimal | None = None
    adapter_side: str | None = None
    adapter_stop_loss: Decimal | None = None
    adapter_take_profit: Decimal | None = None
    # Estado en la DB (None si no existe ahí)
    db_quantity: Decimal | None = None
    db_entry_price: Decimal | None = None
    db_side: str | None = None
    # Estado configurado localmente (PositionManager), cuando aplica
    config_stop_loss: Decimal | None = None
    config_take_profit: Decimal | None = None

    model_config = {"frozen": True}


class OrderDiscrepancy(BaseModel):
    """Discrepancia detectada en el estado de una orden."""

    client_order_id: str
    symbol: str
    discrepancy_type: DiscrepancyType
    detail: str
    adapter_status: str | None = None
    db_status: str | None = None
    quantity_requested: Decimal | None = None
    quantity_filled: Decimal | None = None

    model_config = {"frozen": True}


class ReconciliationReport(BaseModel):
    """Resultado completo de una reconciliación entre adapter y DB."""

    bot_run_id: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    position_discrepancies: list[PositionDiscrepancy] = Field(default_factory=list)
    order_discrepancies: list[OrderDiscrepancy] = Field(default_factory=list)

    model_config = {"frozen": True}

    @property
    def is_consistent(self) -> bool:
        """True si no se encontró ninguna discrepancia."""
        return not self.position_discrepancies and not self.order_discrepancies

    @property
    def total_discrepancies(self) -> int:
        return len(self.position_discrepancies) + len(self.order_discrepancies)


# ---------------------------------------------------------------------------
# Motor de reconciliación
# ---------------------------------------------------------------------------


class ReconciliationEngine:
    """Compara el estado del exchange (vía ExchangeAdapter) con el estado persistido en DB.

    Uso:
        engine = ReconciliationEngine(adapter, position_repo, order_repo, position_manager)
        report = engine.reconcile(bot_run_id)
        if not report.is_consistent:
            # inspeccionar report.position_discrepancies / report.order_discrepancies
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        position_repo: PositionRepository,
        order_repo: OrderRepository,
        position_manager: PositionManager | None = None,
        symbols: frozenset[str] | None = None,
        decimal_tolerance: Decimal = _DEFAULT_DECIMAL_TOLERANCE,
    ) -> None:
        self._adapter = adapter
        self._position_repo = position_repo
        self._order_repo = order_repo
        self._position_manager = position_manager
        self._symbols = tuple(sorted(symbols or ALLOWED_SYMBOLS))
        self._decimal_tolerance = decimal_tolerance

    def reconcile(self, bot_run_id: str) -> ReconciliationReport:
        """Ejecuta la reconciliación completa y retorna el reporte.

        Compara, símbolo por símbolo:
        1. Posiciones abiertas en el exchange vs posiciones OPEN en DB (+ protección).
        2. Órdenes vivas en el exchange vs órdenes PENDING registradas en DB.
        """
        pos_discrepancies = self._reconcile_positions(bot_run_id)
        order_discrepancies = self._reconcile_orders(bot_run_id)

        report = ReconciliationReport(
            bot_run_id=bot_run_id,
            position_discrepancies=pos_discrepancies,
            order_discrepancies=order_discrepancies,
        )

        _log.info(
            "reconciliation.complete",
            bot_run_id=bot_run_id,
            is_consistent=report.is_consistent,
            position_discrepancies=len(pos_discrepancies),
            order_discrepancies=len(order_discrepancies),
        )
        return report

    # ------------------------------------------------------------------
    # Reconciliación de posiciones
    # ------------------------------------------------------------------

    def _reconcile_positions(self, bot_run_id: str) -> list[PositionDiscrepancy]:
        discrepancies: list[PositionDiscrepancy] = []
        db_positions = {p.symbol: p for p in self._position_repo.list_open(bot_run_id)}

        for symbol in self._symbols:
            try:
                adapter_pos = self._adapter.get_position(symbol)
            except Exception:
                _log.error("reconciliation.position_fetch_failed", symbol=symbol, exc_info=True)
                continue

            db_pos = db_positions.get(symbol)

            if adapter_pos is None and db_pos is None:
                continue

            if adapter_pos is not None and db_pos is None:
                discrepancies.append(
                    PositionDiscrepancy(
                        symbol=symbol,
                        discrepancy_type=DiscrepancyType.MISSING_IN_DB,
                        detail=f"Position for {symbol} exists in adapter but not in DB (OPEN).",
                        adapter_quantity=adapter_pos.quantity,
                        adapter_entry_price=adapter_pos.entry_price,
                        adapter_side=adapter_pos.side.value,
                        adapter_stop_loss=adapter_pos.stop_loss,
                        adapter_take_profit=adapter_pos.take_profit,
                    )
                )
                continue

            if adapter_pos is None and db_pos is not None:
                discrepancies.append(
                    PositionDiscrepancy(
                        symbol=symbol,
                        discrepancy_type=DiscrepancyType.MISSING_IN_ADAPTER,
                        detail=f"Position for {symbol} is OPEN in DB but missing in adapter.",
                        db_quantity=db_pos.quantity,
                        db_entry_price=db_pos.entry_price,
                        db_side=db_pos.direction,
                    )
                )
                continue

            assert adapter_pos is not None and db_pos is not None
            discrepancies.extend(self._compare_position(symbol, adapter_pos, db_pos))
            discrepancies.extend(self._check_protection(symbol, adapter_pos))

        return discrepancies

    def _compare_position(
        self,
        symbol: str,
        adapter_pos: PositionState,
        db_pos: DbPosition,
    ) -> list[PositionDiscrepancy]:
        found: list[PositionDiscrepancy] = []

        adapter_side = adapter_pos.side.value
        db_side = db_pos.direction
        if adapter_side != db_side:
            found.append(
                PositionDiscrepancy(
                    symbol=symbol,
                    discrepancy_type=DiscrepancyType.SIDE_MISMATCH,
                    detail=(f"Side mismatch for {symbol}: adapter={adapter_side}, db={db_side}."),
                    adapter_side=adapter_side,
                    db_side=db_side,
                )
            )

        if abs(adapter_pos.quantity - db_pos.quantity) > self._decimal_tolerance:
            found.append(
                PositionDiscrepancy(
                    symbol=symbol,
                    discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
                    detail=(
                        f"Quantity mismatch for {symbol}: "
                        f"adapter={adapter_pos.quantity}, db={db_pos.quantity}."
                    ),
                    adapter_quantity=adapter_pos.quantity,
                    db_quantity=db_pos.quantity,
                )
            )

        if abs(adapter_pos.entry_price - db_pos.entry_price) > self._decimal_tolerance:
            found.append(
                PositionDiscrepancy(
                    symbol=symbol,
                    discrepancy_type=DiscrepancyType.PRICE_MISMATCH,
                    detail=(
                        f"Entry price mismatch for {symbol}: "
                        f"adapter={adapter_pos.entry_price}, db={db_pos.entry_price}."
                    ),
                    adapter_entry_price=adapter_pos.entry_price,
                    db_entry_price=db_pos.entry_price,
                )
            )

        return found

    def _check_protection(
        self, symbol: str, adapter_pos: PositionState
    ) -> list[PositionDiscrepancy]:
        """Valida protección local (PositionConfig) y, si el adapter la reporta, SL/TP del exchange.

        No-op si no se inyectó un PositionManager (compatible con el uso previo
        sin este chequeo).
        """
        if self._position_manager is None:
            return []

        found: list[PositionDiscrepancy] = []
        config = self._position_manager.get_config(symbol)

        if config is None:
            found.append(
                PositionDiscrepancy(
                    symbol=symbol,
                    discrepancy_type=DiscrepancyType.MISSING_PROTECTION,
                    detail=(
                        f"Position {symbol} is open but has no active PositionConfig "
                        "watching it (SL/TP monitoring lost)."
                    ),
                    adapter_side=adapter_pos.side.value,
                    adapter_quantity=adapter_pos.quantity,
                )
            )
            return found

        found.extend(
            self._check_manual_sl_tp_change(
                symbol, "stop_loss", adapter_pos.stop_loss, config.stop_loss
            )
        )
        found.extend(
            self._check_manual_sl_tp_change(
                symbol, "take_profit", adapter_pos.take_profit, config.take_profit
            )
        )
        return found

    def _check_manual_sl_tp_change(
        self,
        symbol: str,
        field_name: str,
        adapter_value: Decimal | None,
        config_value: Decimal | None,
    ) -> list[PositionDiscrepancy]:
        # Solo aplica cuando el exchange reporta un valor propio para ese campo
        # (algunos adapters reales lo hacen; PaperAdapter nunca).
        if adapter_value is None:
            return []
        if (
            config_value is not None
            and abs(adapter_value - config_value) <= self._decimal_tolerance
        ):
            return []

        return [
            PositionDiscrepancy(
                symbol=symbol,
                discrepancy_type=DiscrepancyType.MANUAL_SL_TP_CHANGE,
                detail=(
                    f"{field_name} for {symbol} on exchange ({adapter_value}) does not match "
                    f"local PositionConfig ({config_value}) — likely changed manually."
                ),
                adapter_stop_loss=adapter_value if field_name == "stop_loss" else None,
                adapter_take_profit=adapter_value if field_name == "take_profit" else None,
                config_stop_loss=config_value if field_name == "stop_loss" else None,
                config_take_profit=config_value if field_name == "take_profit" else None,
            )
        ]

    # ------------------------------------------------------------------
    # Reconciliación de órdenes
    # ------------------------------------------------------------------

    def _reconcile_orders(self, bot_run_id: str) -> list[OrderDiscrepancy]:
        discrepancies: list[OrderDiscrepancy] = []

        db_pending: dict[str, DbOrder] = {
            o.client_order_id: o for o in self._order_repo.list_by_status(bot_run_id, "PENDING")
        }
        seen_adapter_coids: set[str] = set()

        for symbol in self._symbols:
            try:
                adapter_orders = self._adapter.get_open_orders(symbol)
            except Exception:
                _log.error("reconciliation.orders_fetch_failed", symbol=symbol, exc_info=True)
                continue

            if not adapter_orders:
                continue

            known_ids = self._order_repo.list_known_client_order_ids(
                [o.client_order_id for o in adapter_orders]
            )

            for adapter_order in adapter_orders:
                seen_adapter_coids.add(adapter_order.client_order_id)
                discrepancies.extend(self._compare_order(adapter_order, db_pending, known_ids))

        # Órdenes PENDING en DB que ya no están vivas en el exchange (se
        # resolvieron fuera del bot: fill o cancelación manual).
        for coid, db_order in db_pending.items():
            if coid not in seen_adapter_coids:
                discrepancies.append(
                    OrderDiscrepancy(
                        client_order_id=coid,
                        symbol=db_order.symbol,
                        discrepancy_type=DiscrepancyType.MISSING_IN_ADAPTER,
                        detail=(
                            f"Order {coid} is PENDING in DB but is no longer open on the exchange."
                        ),
                        db_status=db_order.status,
                    )
                )

        return discrepancies

    def _compare_order(
        self,
        adapter_order: OrderResult,
        db_pending: dict[str, DbOrder],
        known_ids: set[str],
    ) -> list[OrderDiscrepancy]:
        coid = adapter_order.client_order_id

        if coid not in known_ids:
            return [
                OrderDiscrepancy(
                    client_order_id=coid,
                    symbol=adapter_order.symbol,
                    discrepancy_type=DiscrepancyType.MISSING_IN_DB,
                    detail=(
                        f"Order {coid} exists in adapter "
                        f"(status={adapter_order.status}) but not in DB."
                    ),
                    adapter_status=adapter_order.status.value,
                )
            ]

        if adapter_order.status == OrderStatus.PARTIALLY_FILLED:
            return [
                OrderDiscrepancy(
                    client_order_id=coid,
                    symbol=adapter_order.symbol,
                    discrepancy_type=DiscrepancyType.PARTIAL_FILL,
                    detail=(
                        f"Order {coid} is partially filled on the exchange: "
                        f"{adapter_order.quantity_filled}/{adapter_order.quantity_requested}."
                    ),
                    adapter_status=adapter_order.status.value,
                    quantity_requested=adapter_order.quantity_requested,
                    quantity_filled=adapter_order.quantity_filled,
                )
            ]

        db_order = db_pending.get(coid)
        if db_order is not None and adapter_order.status.value != db_order.status:
            return [
                OrderDiscrepancy(
                    client_order_id=coid,
                    symbol=adapter_order.symbol,
                    discrepancy_type=DiscrepancyType.STATUS_MISMATCH,
                    detail=(
                        f"Status mismatch for order {coid}: "
                        f"adapter={adapter_order.status.value}, db={db_order.status}."
                    ),
                    adapter_status=adapter_order.status.value,
                    db_status=db_order.status,
                )
            ]

        return []


__all__ = [
    "DiscrepancyType",
    "OrderDiscrepancy",
    "PositionDiscrepancy",
    "ReconciliationEngine",
    "ReconciliationReport",
]
