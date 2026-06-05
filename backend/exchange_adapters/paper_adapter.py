"""PaperAdapter — simula el exchange para el modo PAPER.

Implementa ExchangeAdapter sin llamadas reales a BingX ni a ningún endpoint.
Todas las órdenes se marcan como is_simulated=True.

Modelo de simulación:
- Las órdenes MARKET se completan inmediatamente al precio dado + slippage.
- Las órdenes LIMIT/STOP_MARKET se registran como PENDING (sin auto-fill).
- Fees: taker_fee_rate × notional (default: 0.05%).
- Slippage: slippage_bps × precio (default: 5bps). Aumenta precio en BUY,
  disminuye en SELL.
- Idempotencia: si se recibe un client_order_id ya conocido, retorna el
  resultado previo sin crear duplicados.

Estado: en memoria (sin acceso a DB). La persistencia es responsabilidad
del Execution Engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog

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
            o for o in self._orders.values()
            if o.symbol == symbol and o.status == OrderStatus.PENDING
        ]

    def get_account_state(self) -> AccountState:
        used_margin: Decimal = sum(
            (p.margin_usdt for p in self._positions.values()), Decimal("0")
        )
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

        # Actualizar balance y posición simulada
        if not request.is_reduce_only:
            self._update_position(request, fill_price)
            self._balance_usdt -= fee_usdt

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

    def _update_position(self, request: OrderRequest, fill_price: Decimal) -> None:
        leverage = self._leverage.get(request.symbol, 1)
        notional = fill_price * request.quantity
        margin_usdt = notional / Decimal(leverage)

        position_side = (
            OrderSide.BUY if request.side == OrderSide.BUY else OrderSide.SELL
        )

        self._positions[request.symbol] = PositionState(
            symbol=request.symbol,
            side=position_side,
            quantity=request.quantity,
            entry_price=fill_price,
            unrealized_pnl=Decimal("0"),
            margin_usdt=margin_usdt,
            leverage=leverage,
            is_simulated=True,
        )


def _now() -> datetime:
    return datetime.now(UTC)
