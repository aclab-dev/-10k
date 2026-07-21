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
from decimal import ROUND_UP, Decimal
from typing import Any

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
_QUOTE_BASE_URL = "https://open-api.bingx.com"

# La orden resting debe quedar lejos del precio real (para no fillear) pero dentro
# de la banda de precio que BingX valida contra el mark price actual — un precio
# estático se rompe apenas el mercado se mueve (ver tarjeta [101], BingX rechazó un
# intento con "Order price should be higher than 13270.8"). Por eso el precio se
# calcula en cada test contra el último precio real (endpoint público, sin firma).
_RESTING_PRICE_DISCOUNT = Decimal("0.5")  # 50% por debajo del mark price actual
_TARGET_NOTIONAL_USDT = Decimal("3")  # > tradeMinUSDT (2 USDT, docs/bingx_api_reference.md §7)
_MIN_QUANTITY = Decimal("0.0001")  # tradeMinQuantity BTC-USDT (docs/bingx_api_reference.md §7)


def _current_mark_price(symbol: str) -> Decimal:
    """Último precio público (sin firma) — sólo para calcular un precio resting
    seguro. No reemplaza a BingXDataFetcher (klines/ticker quedan fuera de [101])."""
    bingx_symbol = f"{symbol[:-4]}-USDT"
    response = httpx.get(
        f"{_QUOTE_BASE_URL}/openApi/swap/v2/quote/price",
        params={"symbol": bingx_symbol},
        timeout=10.0,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return Decimal(str(body["data"]["price"]))


def _resting_order_request() -> OrderRequest:
    mark_price = _current_mark_price(_SYMBOL)
    limit_price = (mark_price * _RESTING_PRICE_DISCOUNT).quantize(Decimal("0.1"))
    quantity = max(
        _MIN_QUANTITY,
        (_TARGET_NOTIONAL_USDT / limit_price).quantize(Decimal("0.0001"), rounding=ROUND_UP),
    )
    return OrderRequest(
        symbol=_SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=limit_price,
    )


@pytest.fixture(scope="module")
def adapter() -> Iterator[BingXAdapter]:
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip(
            "BINGX_API_KEY/BINGX_API_SECRET no configuradas — test de integración BingX omitido."
        )

    # No llamamos set_leverage() acá a propósito: BingXAdapter sólo fuerza ONE_WAY
    # mode (positionSide/dual) de forma perezosa dentro de place_order(). Una cuenta
    # demo nueva arranca en Hedge Mode por default de BingX, y set_leverage() asume
    # ONE_WAY ya activo (manda side="BOTH") — llamarlo antes de cualquier place_order()
    # falla con "BingX error 109400: In the Hedge mode, the 'Side' field can only be
    # set to LONG, SHORT or ALL." Dejamos el leverage default de la cuenta; el notional
    # objetivo de _resting_order_request() (~3 USDT) ya es seguro en margen incluso en
    # el peor caso (1x, sin reducción de margen).
    bingx = BingXAdapter(api_key=api_key, api_secret=api_secret, environment=Environment.TESTNET)
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
        # DEBUG TEMPORAL (revertir antes de mergear): forzar el valor real en el log de CI.
        pytest.fail(f"DEBUG account_state={state!r}")

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
