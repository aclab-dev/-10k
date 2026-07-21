"""Tests de integración para BingXAdapter (tarjeta [101]) — requieren cuenta demo real.

BingX no ofrece un host TESTNET separado (ver docs/bingx_api_reference.md §7):
estos tests golpean el mismo host de producción (https://open-api.bingx.com)
usando credenciales de una cuenta demo/sandbox de BingX (fondos virtuales,
separados de cualquier cuenta real).

Requiere BINGX_API_KEY y BINGX_API_SECRET en el entorno (credenciales de la
cuenta demo, con permisos Read + Trade y retiro deshabilitado). Si no están
seteadas, todos los tests de este archivo se saltean.

Ejecutar con:
    BINGX_API_KEY=... BINGX_API_SECRET=... pytest -m integration -k bingx
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import structlog

from backend.core.config import Environment
from backend.exchange_adapters.bingx_adapter import BingXAdapter, BingXApiError
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionState,
)

_log = structlog.get_logger(__name__)

_SYMBOL = "BTCUSDT"
_TEST_LEVERAGE = 2

# Precio y cantidad elegidos para que la orden LIMIT quede lejos del mercado real
# (nunca debería fillear) y a la vez respete tradeMinUSDT: 2 USDT (docs/bingx_api_reference.md
# §7) y el cap de margen del proyecto (≤10 USDT): notional = 0.0005 * 5000 = 2.5 USDT,
# margen a 2x = 1.25 USDT. Precio estático: si BTC llegara a cotizar cerca de este nivel,
# la orden podría fillear — revisar si estos tests empiezan a fallar de forma inesperada.
_SAFE_LIMIT_PRICE = Decimal("5000")
_SAFE_QUANTITY = Decimal("0.0005")


def _resting_order_request() -> OrderRequest:
    return OrderRequest(
        symbol=_SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=_SAFE_QUANTITY,
        price=_SAFE_LIMIT_PRICE,
    )


@pytest.fixture(scope="module")
def adapter() -> Iterator[BingXAdapter]:
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip(
            "BINGX_API_KEY/BINGX_API_SECRET no configuradas — test de integración BingX omitido."
        )

    bingx = BingXAdapter(api_key=api_key, api_secret=api_secret, environment=Environment.TESTNET)
    bingx.set_leverage(_SYMBOL, _TEST_LEVERAGE)
    yield bingx

    # Cleanup defensivo: cancela cualquier orden que haya quedado abierta si un
    # test falló a mitad de camino, para no ensuciar la cuenta demo entre corridas.
    # Un error puntual de la API no debe cortar el barrido del resto de las órdenes.
    for order in bingx.get_open_orders(_SYMBOL):
        try:
            bingx.cancel_order(order.client_order_id)
        except (BingXApiError, httpx.HTTPStatusError) as exc:
            _log.warning(
                "bingx_testnet_cleanup.cancel_failed",
                client_order_id=order.client_order_id,
                error=str(exc),
            )


@pytest.mark.integration
class TestBingXAdapterReadMethods:
    """Lectura contra la cuenta demo real — tarjeta [101]."""

    def test_get_account_state_returns_real_balance(self, adapter: BingXAdapter) -> None:
        state = adapter.get_account_state()
        assert isinstance(state, AccountState)
        # BingXAdapter.get_account_state() siempre hardcodea is_simulated=False
        # (a diferencia de PaperAdapter) — no depende de Environment.TESTNET.
        assert state.is_simulated is False

    def test_get_position_returns_none_or_valid_state(self, adapter: BingXAdapter) -> None:
        position = adapter.get_position(_SYMBOL)
        assert position is None or isinstance(position, PositionState)

    def test_get_open_orders_returns_list(self, adapter: BingXAdapter) -> None:
        orders = adapter.get_open_orders(_SYMBOL)
        assert isinstance(orders, list)


@pytest.mark.integration
class TestBingXAdapterOrderLifecycle:
    """Envío de órdenes contra la cuenta demo real — tarjeta [101]."""

    def test_place_order_and_cancel_roundtrip(self, adapter: BingXAdapter) -> None:
        request = _resting_order_request()

        result = adapter.place_order(request)
        try:
            assert result.client_order_id == request.client_order_id
            assert result.status == OrderStatus.PENDING

            status = adapter.get_order_status(request.client_order_id)
            assert status is not None
            assert status.status == OrderStatus.PENDING
        finally:
            # Cleanup incondicional: no assertar acá para no enmascarar un fallo
            # real del bloque try con un AssertionError distinto en el finally.
            adapter.cancel_order(request.client_order_id)


@pytest.mark.integration
class TestBingXAdapterIdempotency:
    """Idempotencia por clientOrderId contra el exchange real — tarjeta [101].

    Los tests unitarios (tests/unit/test_bingx_adapter.py) sólo verifican que no
    se dispara un segundo POST; esto verifica que BingX no crea una orden
    duplicada del lado del exchange real.
    """

    def test_place_order_is_idempotent_against_real_exchange(self, adapter: BingXAdapter) -> None:
        request = _resting_order_request()

        first = adapter.place_order(request)
        try:
            second = adapter.place_order(request)
            assert second.order_id == first.order_id
            assert second.client_order_id == first.client_order_id
        finally:
            adapter.cancel_order(request.client_order_id)
