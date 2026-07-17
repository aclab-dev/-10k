"""BingXAdapter — implementación de ExchangeAdapter contra BingX Futures (USDT-M).

Esta clase prueba que la interfaz ExchangeAdapter (base.py) es implementable
por un adapter real, no solo por PaperAdapter. La lógica de lectura (balance,
posiciones) llega en la tarjeta [98], el envío de órdenes en [99] y la
idempotencia end-to-end con clientOrderId en [100] — acá cada método está
stubbeado con NotImplementedError.

Notas de la API de BingX (ver docs/bingx_api_reference.md):
- No existe un host de TESTNET separado: BingX no ofrece sandbox con URL
  distinta a producción (https://open-api.bingx.com para todos los entornos).
- El formato de símbolo de BingX es "BTC-USDT" (con guión), distinto del
  formato interno del proyecto "BTCUSDT" (ALLOWED_SYMBOLS en schemas.py).
  La traducción entre ambos formatos es responsabilidad de la implementación
  real (tarjetas [98]/[99]), no de esta interfaz.
"""

from __future__ import annotations

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    PositionState,
)


class BingXAdapter(ExchangeAdapter):
    """Adapter contra BingX Futures (USDT-M). Ver docs/bingx_api_reference.md."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        environment: Environment = Environment.TESTNET,
    ) -> None:
        # api_secret nunca debe loguearse ni incluirse en excepciones.
        self._api_key = api_key
        self._api_secret = api_secret
        self._environment = environment

    @property
    def environment(self) -> Environment:
        return self._environment

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError("BingXAdapter.place_order: implementado en la tarjeta [99]")

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError("BingXAdapter.cancel_order: implementado en la tarjeta [99]")

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        raise NotImplementedError("BingXAdapter.get_order_status: implementado en la tarjeta [98]")

    def get_position(self, symbol: str) -> PositionState | None:
        raise NotImplementedError("BingXAdapter.get_position: implementado en la tarjeta [98]")

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        raise NotImplementedError("BingXAdapter.get_open_orders: implementado en la tarjeta [98]")

    def get_account_state(self) -> AccountState:
        raise NotImplementedError("BingXAdapter.get_account_state: implementado en la tarjeta [98]")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError("BingXAdapter.set_leverage: implementado en la tarjeta [99]")

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        raise NotImplementedError("BingXAdapter.set_margin_type: implementado en la tarjeta [99]")
