"""ReconciliationEngine — detecta inconsistencias entre PaperAdapter y la DB.

El PaperAdapter mantiene el estado simulado en memoria (posiciones, órdenes).
La DB persiste ese estado a través de los repositorios de positions y orders.
Este módulo compara ambas fuentes y produce un ReconciliationReport con todas
las discrepancias encontradas.

Scope:
- Solo detecta. No corrige ni muta el adapter ni la DB.
- Solo aplica a posiciones OPEN y órdenes activas (PENDING/FILLED/CANCELLED)
  del bot_run_id dado.
- Discrepancias de precio se evalúan con una tolerancia configurable para
  evitar falsos positivos por redondeo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

import structlog
from pydantic import BaseModel, Field

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import PositionState
from backend.storage.models import Position as DbPosition
from backend.storage.repositories.trades import OrderRepository, PositionRepository

_log = structlog.get_logger(__name__)

# Tolerancia para comparar precios/cantidades: diferencias menores se ignoran.
_DEFAULT_PRICE_TOLERANCE = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Enums y schemas del reporte
# ---------------------------------------------------------------------------


class DiscrepancyType(StrEnum):
    MISSING_IN_DB = "MISSING_IN_DB"
    MISSING_IN_ADAPTER = "MISSING_IN_ADAPTER"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    STATUS_MISMATCH = "STATUS_MISMATCH"


class PositionDiscrepancy(BaseModel):
    """Discrepancia detectada en el estado de una posición."""

    symbol: str
    discrepancy_type: DiscrepancyType
    # Descripción legible del problema
    detail: str
    # Estado en el adapter (None si no existe ahí)
    adapter_quantity: Decimal | None = None
    adapter_entry_price: Decimal | None = None
    adapter_side: str | None = None
    # Estado en la DB (None si no existe ahí)
    db_quantity: Decimal | None = None
    db_entry_price: Decimal | None = None
    db_side: str | None = None

    model_config = {"frozen": True}


class OrderDiscrepancy(BaseModel):
    """Discrepancia detectada en el estado de una orden."""

    client_order_id: str
    symbol: str
    discrepancy_type: DiscrepancyType
    detail: str
    adapter_status: str | None = None
    db_status: str | None = None

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
    """Compara el estado en memoria del PaperAdapter con el estado persistido en DB.

    Uso:
        engine = ReconciliationEngine(adapter, position_repo, order_repo)
        report = engine.reconcile(bot_run_id)
        if not report.is_consistent:
            # inspeccionar report.position_discrepancies / report.order_discrepancies
    """

    def __init__(
        self,
        adapter: PaperAdapter,
        position_repo: PositionRepository,
        order_repo: OrderRepository,
        price_tolerance: Decimal = _DEFAULT_PRICE_TOLERANCE,
    ) -> None:
        self._adapter = adapter
        self._position_repo = position_repo
        self._order_repo = order_repo
        self._price_tolerance = price_tolerance

    def reconcile(self, bot_run_id: str) -> ReconciliationReport:
        """Ejecuta la reconciliación completa y retorna el reporte.

        Compara:
        1. Posiciones abiertas en el adapter vs posiciones OPEN en DB.
        2. Órdenes conocidas por el adapter vs órdenes registradas en DB.
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

        adapter_positions: dict[str, PositionState] = dict(self._adapter._positions)
        db_positions = {p.symbol: p for p in self._position_repo.list_open(bot_run_id)}

        # Posiciones que el adapter tiene pero que no están en DB
        for symbol, adapter_pos in adapter_positions.items():
            if symbol not in db_positions:
                discrepancies.append(
                    PositionDiscrepancy(
                        symbol=symbol,
                        discrepancy_type=DiscrepancyType.MISSING_IN_DB,
                        detail=f"Position for {symbol} exists in adapter but not in DB (OPEN).",
                        adapter_quantity=adapter_pos.quantity,
                        adapter_entry_price=adapter_pos.entry_price,
                        adapter_side=adapter_pos.side.value,
                    )
                )
                continue

            db_pos = db_positions[symbol]
            discrepancies.extend(self._compare_position(symbol, adapter_pos, db_pos))

        # Posiciones OPEN en DB que el adapter ya no tiene
        for symbol, db_pos in db_positions.items():
            if symbol not in adapter_positions:
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

        return discrepancies

    def _compare_position(
        self,
        symbol: str,
        adapter_pos: PositionState,
        db_pos: DbPosition,
    ) -> list[PositionDiscrepancy]:
        found: list[PositionDiscrepancy] = []

        if abs(adapter_pos.quantity - db_pos.quantity) > self._price_tolerance:
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

        if abs(adapter_pos.entry_price - db_pos.entry_price) > self._price_tolerance:
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

    # ------------------------------------------------------------------
    # Reconciliación de órdenes
    # ------------------------------------------------------------------

    def _reconcile_orders(self, bot_run_id: str) -> list[OrderDiscrepancy]:
        discrepancies: list[OrderDiscrepancy] = []

        adapter_orders = dict(self._adapter._orders)  # client_order_id → OrderResult

        # Órdenes PENDING en DB: pueden haber sido actualizadas en el adapter sin que la DB lo sepa
        db_pending = self._order_repo.list_by_status(bot_run_id, "PENDING", limit=1000)
        db_filled = self._order_repo.list_by_status(bot_run_id, "FILLED", limit=1000)
        db_cancelled = self._order_repo.list_by_status(bot_run_id, "CANCELLED", limit=1000)

        db_orders = {o.client_order_id: o for o in [*db_pending, *db_filled, *db_cancelled]}

        # Órdenes en adapter que no están en DB
        for coid, adapter_order in adapter_orders.items():
            if coid not in db_orders:
                discrepancies.append(
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
                )
                continue

            db_order = db_orders[coid]
            adapter_status = adapter_order.status.value
            db_status = db_order.status

            # Normalizar status de DB al mismo espacio que el adapter
            if adapter_status != db_status:
                discrepancies.append(
                    OrderDiscrepancy(
                        client_order_id=coid,
                        symbol=adapter_order.symbol,
                        discrepancy_type=DiscrepancyType.STATUS_MISMATCH,
                        detail=(
                            f"Status mismatch for order {coid}: "
                            f"adapter={adapter_status}, db={db_status}."
                        ),
                        adapter_status=adapter_status,
                        db_status=db_status,
                    )
                )

        # Órdenes en DB que el adapter no conoce
        for coid, db_order in db_orders.items():
            if coid not in adapter_orders:
                discrepancies.append(
                    OrderDiscrepancy(
                        client_order_id=coid,
                        symbol=db_order.symbol,
                        discrepancy_type=DiscrepancyType.MISSING_IN_ADAPTER,
                        detail=(
                            f"Order {coid} is in DB "
                            f"(status={db_order.status}) but unknown to adapter."
                        ),
                        db_status=db_order.status,
                    )
                )

        return discrepancies
