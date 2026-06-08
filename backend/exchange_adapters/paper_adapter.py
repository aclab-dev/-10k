"""PaperAdapter — simula el exchange para el modo PAPER.

Implementa ExchangeAdapter sin llamadas reales a BingX ni a ningún endpoint.
Todas las órdenes se marcan como is_simulated=True.

Modelo de simulación:
- Las órdenes MARKET se completan inmediatamente al precio dado + slippage.
- Las órdenes LIMIT/STOP_MARKET se registran como PENDING (sin auto-fill).
- Fees: taker_fee_rate × notional (default: 0.05%). Se descuenta siempre,
  incluso en órdenes de cierre (is_reduce_only=True).
- Slippage: slippage_bps × precio (default: 5bps). Aumenta precio en BUY,
  disminuye en SELL.
- Idempotencia: si se recibe un client_order_id ya conocido, retorna el
  resultado previo sin crear duplicados.
- Posiciones: modo ONE_WAY. Órdenes en la misma dirección netean (weighted
  average entry). Órdenes is_reduce_only cierran o reducen la posición y
  realizan PnL.

Estado: en memoria (sin acceso a DB). La persistencia es responsabilidad
del Execution Engine.

Nota de thread safety: los dicts internos (_orders, _positions, _leverage)
no tienen locks. El adapter asume ejecución single-threaded o que el caller
serializa el acceso. No usar desde múltiples corrutinas concurrentes sin
sincronización externa.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog

from backend.backtesting.funding_model import compute_funding_payment
from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionState,
)

_log = structlog.get_logger(__name__)

# Fee taker para futuros perpetuos (BingX referencia: ~0.05%)
_DEFAULT_TAKER_FEE_RATE = Decimal("0.0005")

# Slippage en basis points (1 bps = 0.01%)
_DEFAULT_SLIPPAGE_BPS = Decimal("5")

_BPS_DIVISOR = Decimal("10000")

# Leverage máximo permitido en PAPER
_MAX_LEVERAGE_PAPER = 10


class PaperAdapter(ExchangeAdapter):
    """Adapter de simulación para el modo PAPER."""

    def __init__(
        self,
        initial_balance_usdt: Decimal = Decimal("1000"),
        taker_fee_rate: Decimal = _DEFAULT_TAKER_FEE_RATE,
        slippage_bps: Decimal = _DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        self._balance_usdt = initial_balance_usdt
        self._taker_fee_rate = taker_fee_rate
        self._slippage_bps = slippage_bps

        # Keyed by client_order_id
        self._orders: dict[str, OrderResult] = {}
        # Keyed by symbol
        self._positions: dict[str, PositionState] = {}
        # Keyed by symbol → leverage
        self._leverage: dict[str, int] = {}
        # Acumulado de funding pagado por símbolo (positivo = pagamos, negativo = recibimos)
        self._funding_paid: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # ExchangeAdapter protocol
    # ------------------------------------------------------------------

    @property
    def environment(self) -> Environment:
        return Environment.PAPER

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.client_order_id in self._orders:
            _log.info(
                "paper_adapter.idempotent_order",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
            )
            return self._orders[request.client_order_id]

        if request.order_type == OrderType.MARKET:
            result = self._fill_market_order(request)
        else:
            result = self._register_pending_order(request)

        self._orders[request.client_order_id] = result
        _log.info(
            "paper_adapter.order_placed",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status=result.status,
            fill_price=str(result.fill_price),
            fee_usdt=str(result.fee_usdt),
        )
        return result

    def cancel_order(self, client_order_id: str) -> bool:
        result = self._orders.get(client_order_id)
        if result is None:
            return False
        if result.status != OrderStatus.PENDING:
            return False

        cancelled = OrderResult(
            order_id=result.order_id,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            side=result.side,
            order_type=result.order_type,
            status=OrderStatus.CANCELLED,
            quantity_requested=result.quantity_requested,
            quantity_filled=Decimal("0"),
            fill_price=None,
            fee_usdt=Decimal("0"),
            slippage_usdt=Decimal("0"),
            is_simulated=True,
            timestamp_utc=_now(),
        )
        self._orders[client_order_id] = cancelled
        _log.info("paper_adapter.order_cancelled", client_order_id=client_order_id)
        return True

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        return self._orders.get(client_order_id)

    def get_position(self, symbol: str) -> PositionState | None:
        return self._positions.get(symbol)

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        return [
            o
            for o in self._orders.values()
            if o.symbol == symbol and o.status == OrderStatus.PENDING
        ]

    def get_account_state(self) -> AccountState:
        used_margin: Decimal = sum((p.margin_usdt for p in self._positions.values()), Decimal("0"))
        unrealized_pnl: Decimal = sum(
            (p.unrealized_pnl for p in self._positions.values()), Decimal("0")
        )
        equity = self._balance_usdt + unrealized_pnl
        available = max(Decimal("0"), equity - used_margin)
        return AccountState(
            balance_usdt=self._balance_usdt,
            equity_usdt=equity,
            available_margin_usdt=available,
            used_margin_usdt=used_margin,
            is_simulated=True,
        )

    def set_leverage(self, symbol: str, leverage: int) -> None:
        if leverage < 1 or leverage > _MAX_LEVERAGE_PAPER:
            raise ValueError(
                f"Leverage {leverage}x fuera del rango permitido en PAPER "
                f"(1–{_MAX_LEVERAGE_PAPER}x)"
            )
        self._leverage[symbol] = leverage
        _log.info("paper_adapter.leverage_set", symbol=symbol, leverage=leverage)

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        if margin_type == MarginType.CROSS:
            raise ValueError("Cross margin está prohibido. Solo se permite ISOLATED.")
        _log.info("paper_adapter.margin_type_set", symbol=symbol, margin_type=margin_type)

    def apply_funding(self, symbol: str, funding_rate: Decimal, mark_price: Decimal) -> Decimal:
        """Aplica el funding rate a la posición abierta en `symbol`.

        Retorna el pago de funding (positivo = pagamos, negativo = recibimos).
        Si no hay posición abierta, retorna Decimal("0") sin tocar el balance.
        """
        position = self._positions.get(symbol)
        if position is None:
            return Decimal("0")

        payment = compute_funding_payment(
            side=position.side,
            quantity=position.quantity,
            mark_price=mark_price,
            funding_rate=funding_rate,
        )

        self._balance_usdt -= payment
        self._funding_paid[symbol] = self._funding_paid.get(symbol, Decimal("0")) + payment

        _log.info(
            "paper_adapter.funding_applied",
            symbol=symbol,
            funding_rate=str(funding_rate),
            mark_price=str(mark_price),
            position_side=position.side,
            quantity=str(position.quantity),
            funding_payment_usdt=str(payment),
            accumulated_funding_usdt=str(self._funding_paid[symbol]),
        )
        return payment

    def get_funding_paid(self, symbol: str) -> Decimal:
        """Retorna el funding acumulado pagado para `symbol` en esta sesión.

        Positivo = pagamos en total, negativo = recibimos en total.
        """
        return self._funding_paid.get(symbol, Decimal("0"))

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _fill_market_order(self, request: OrderRequest) -> OrderResult:
        if request.price is None or request.price <= Decimal("0"):
            raise ValueError(
                f"place_order MARKET requiere price > 0 para simular el fill. "
                f"client_order_id={request.client_order_id}"
            )

        slippage_amount = request.price * self._slippage_bps / _BPS_DIVISOR

        if request.side == OrderSide.BUY:
            fill_price = request.price + slippage_amount
        else:
            fill_price = request.price - slippage_amount

        notional = fill_price * request.quantity
        fee_usdt = notional * self._taker_fee_rate
        slippage_usdt = abs(slippage_amount * request.quantity)

        # Fee se descuenta siempre, independientemente de is_reduce_only
        self._balance_usdt -= fee_usdt

        if request.is_reduce_only:
            self._close_or_reduce_position(request.symbol, request.quantity, fill_price)
        else:
            self._open_or_net_position(request, fill_price)

        return OrderResult(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            quantity_requested=request.quantity,
            quantity_filled=request.quantity,
            fill_price=fill_price,
            fee_usdt=fee_usdt,
            slippage_usdt=slippage_usdt,
            is_simulated=True,
            timestamp_utc=_now(),
        )

    def _register_pending_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.PENDING,
            quantity_requested=request.quantity,
            quantity_filled=Decimal("0"),
            fill_price=None,
            fee_usdt=Decimal("0"),
            slippage_usdt=Decimal("0"),
            is_simulated=True,
            timestamp_utc=_now(),
        )

    def _open_or_net_position(self, request: OrderRequest, fill_price: Decimal) -> None:
        """Abre una nueva posición o netea con la existente en la misma dirección.

        En modo ONE_WAY, si ya hay posición abierta en la misma dirección,
        recalcula el entry_price como promedio ponderado y suma los márgenes.
        """
        leverage = self._leverage.get(request.symbol, 1)
        new_notional = fill_price * request.quantity
        new_margin = new_notional / Decimal(leverage)

        existing = self._positions.get(request.symbol)

        if existing is not None and existing.side != request.side:
            _log.warning(
                "paper_adapter.side_flip_without_reduce_only",
                symbol=request.symbol,
                existing_side=existing.side,
                new_side=request.side,
                msg="Flip de lado sin is_reduce_only en ONE_WAY — el margen previo no se devuelve",
            )

        if existing is None or existing.side != request.side:
            # Nueva posición (o cambio de lado; en ONE_WAY esto no debería ocurrir)
            self._positions[request.symbol] = PositionState(
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                entry_price=fill_price,
                unrealized_pnl=Decimal("0"),
                margin_usdt=new_margin,
                leverage=leverage,
                is_simulated=True,
            )
        else:
            # Neteo: promedio ponderado del entry_price
            total_quantity = existing.quantity + request.quantity
            weighted_entry = (
                existing.entry_price * existing.quantity + fill_price * request.quantity
            ) / total_quantity
            total_margin = existing.margin_usdt + new_margin

            self._positions[request.symbol] = PositionState(
                symbol=request.symbol,
                side=existing.side,
                quantity=total_quantity,
                entry_price=weighted_entry,
                unrealized_pnl=Decimal("0"),
                margin_usdt=total_margin,
                leverage=leverage,
                is_simulated=True,
            )

    def _close_or_reduce_position(
        self, symbol: str, quantity: Decimal, fill_price: Decimal
    ) -> None:
        """Cierra o reduce una posición existente y realiza el PnL.

        Si quantity >= posición total → cierre completo.
        Si quantity < posición total → reducción parcial.
        """
        existing = self._positions.get(symbol)
        if existing is None:
            _log.warning(
                "paper_adapter.reduce_only_no_position",
                symbol=symbol,
                quantity=str(quantity),
            )
            return

        # PnL por unidad según dirección de la posición
        if existing.side == OrderSide.BUY:
            pnl_per_unit = fill_price - existing.entry_price
        else:
            pnl_per_unit = existing.entry_price - fill_price

        closed_qty = min(quantity, existing.quantity)
        realized_pnl = pnl_per_unit * closed_qty

        # Actualizar balance con el PnL realizado
        self._balance_usdt += realized_pnl

        if closed_qty >= existing.quantity:
            # Cierre total
            del self._positions[symbol]
            _log.info(
                "paper_adapter.position_closed",
                symbol=symbol,
                realized_pnl=str(realized_pnl),
            )
        else:
            # Reducción parcial — mantener el entry_price original
            remaining_qty = existing.quantity - closed_qty
            leverage = self._leverage.get(symbol, existing.leverage)
            remaining_margin = (existing.entry_price * remaining_qty) / Decimal(leverage)

            self._positions[symbol] = PositionState(
                symbol=symbol,
                side=existing.side,
                quantity=remaining_qty,
                entry_price=existing.entry_price,
                unrealized_pnl=Decimal("0"),
                margin_usdt=remaining_margin,
                leverage=existing.leverage,
                is_simulated=True,
            )
            _log.info(
                "paper_adapter.position_reduced",
                symbol=symbol,
                closed_qty=str(closed_qty),
                remaining_qty=str(remaining_qty),
                realized_pnl=str(realized_pnl),
            )


def _now() -> datetime:
    return datetime.now(UTC)
