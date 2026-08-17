"""BingXAdapter — implementación de ExchangeAdapter contra BingX Futures (USDT-M).

Métodos de lectura (tarjeta [98]):
  - get_account_state  →  GET /openApi/swap/v2/user/balance
  - get_position       →  GET /openApi/swap/v2/user/positions
  - get_open_orders    →  GET /openApi/swap/v2/trade/openOrders
  - get_order_status   →  GET /openApi/swap/v2/trade/order

Métodos de escritura (tarjeta [99]):
  - place_order        →  POST   /openApi/swap/v2/trade/order
  - cancel_order       →  DELETE /openApi/swap/v2/trade/order
  - set_leverage       →  POST   /openApi/swap/v2/trade/leverage
  - set_margin_type    →  POST   /openApi/swap/v2/trade/marginType

place_order fuerza ONE_WAY (POST /openApi/swap/v1/positionSide/dual) e ISOLATED
(vía set_margin_type) antes de la primera orden — una vez por instancia y por
símbolo respectivamente (ver docs/bingx_api_reference.md §9.4).

El bloqueo de ENVIRONMENT=LIVE (Environment Guard / Execution Engine, PDF 01.1 §4.5)
no vive en este adapter — su única responsabilidad es abstraer el exchange.

Notas de la API de BingX (ver docs/bingx_api_reference.md):
- Host por entorno: LIVE → open-api.bingx.com, TESTNET/PAPER → open-api-vst.bingx.com
  (VST/demo, fondos virtuales). Ver _BASE_URL_BY_ENV.
- El formato de símbolo de BingX es "BTC-USDT" (con guión), distinto del
  formato interno del proyecto "BTCUSDT" (ALLOWED_SYMBOLS en schemas.py).
- Firma: HMAC-SHA256(api_secret, query_string).hexdigest() + X-BX-APIKEY header.
- En ONE_WAY mode, positionSide=BOTH; el signo de positionAmt indica dirección.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from backend.core.config import Environment, MarginType
from backend.core.retry import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    RetryConfig,
    retry_sync,
)
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
from backend.market_data.schemas import ALLOWED_SYMBOLS

_log = structlog.get_logger(__name__)

# BingX expone un host de demo/sandbox (VST — Virtual Simulated Trading) separado
# del de producción, con fondos virtuales y balance propio. Confirmado contra la
# implementación de ccxt (github.com/ccxt/ccxt): urls['api'] vs urls['test'].
# LIVE opera contra producción real; TESTNET y PAPER usan el host VST para no tocar
# fondos reales. Corrige la afirmación errónea de docs/bingx_api_reference.md §7
# ("no existe host de TESTNET separado"), detectada en la tarjeta [101].
_PROD_BASE_URL = "https://open-api.bingx.com"
_VST_BASE_URL = "https://open-api-vst.bingx.com"

_BASE_URL_BY_ENV: dict[Environment, str] = {
    Environment.PAPER: _VST_BASE_URL,
    Environment.TESTNET: _VST_BASE_URL,
    Environment.LIVE: _PROD_BASE_URL,
}

_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.PENDING,
    "PENDING": OrderStatus.PENDING,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "FAILED": OrderStatus.FAILED,
    "EXPIRED": OrderStatus.CANCELLED,
}

_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "MARKET": OrderType.MARKET,
    "LIMIT": OrderType.LIMIT,
    "STOP_MARKET": OrderType.STOP_MARKET,
    "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
}

# Leverage máximo permitido por entorno (PDF 01.1 §4.9: PAPER 10x, TESTNET 5x, LIVE absoluto 5x).
# El tope de 3x "LIVE inicial" es un límite operativo del checklist F17, no de esta clase.
_MAX_LEVERAGE_BY_ENV: dict[Environment, int] = {
    Environment.PAPER: 10,
    Environment.TESTNET: 5,
    Environment.LIVE: 5,
}

# Retry de transporte (F16): solo cubre fallos de red/timeout/5xx en _signed_request.
# Errores de negocio (BingXApiError, `code != 0`, y 4xx) nunca son retryable — se
# propagan igual que antes de F16. place_order/cancel_order siguen siendo seguros ante
# estos reintentos porque BingX dedupe por clientOrderID (ver docstring del módulo).
_RETRY_CONFIG = RetryConfig(max_attempts=4, base_delay_seconds=0.5, max_delay_seconds=8.0)


def is_retryable_bingx_error(exc: Exception) -> bool:
    """True solo para fallos de transporte: sin respuesta, timeout, o 5xx.

    httpx.TimeoutException es subclase de httpx.RequestError, así que un solo
    isinstance cubre ambos. httpx.HTTPStatusError es un tipo hermano (no hereda de
    RequestError) y se evalúa aparte por status_code.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError)


def _to_bingx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT (todos los símbolos permitidos terminan en USDT)."""
    return f"{symbol[:-4]}-USDT"


def _extract_order(data: dict[str, Any]) -> dict[str, Any]:
    """Desanida la orden de la respuesta de BingX.

    GET /trade/order (consulta de una orden) y POST /trade/order envuelven la orden
    bajo la clave "order" (data.order). Igual que ccxt (safe_dict(data, 'order', data)),
    con fallback a `data` por si algún endpoint la devuelve al nivel superior.
    Detectado en la tarjeta [101]: sin esto, _parse_order recibía {"order": {...}} y
    fallaba con KeyError('side').
    """
    order = data.get("order")
    return order if isinstance(order, dict) else data


class BingXApiError(Exception):
    """Raised when BingX API returns a non-zero error code."""


class BingXAdapter(ExchangeAdapter):
    """Adapter contra BingX Futures (USDT-M). Ver docs/bingx_api_reference.md.

    Nota de thread safety: `_order_symbol_cache`, `_one_way_verified` y
    `_isolated_verified_symbols` no tienen locks. El adapter asume ejecución
    single-threaded o que el caller (Execution Engine) serializa las llamadas
    de escritura (place_order, cancel_order, set_leverage, set_margin_type).
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        environment: Environment = Environment.TESTNET,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        # api_secret nunca debe loguearse ni incluirse en excepciones.
        self._api_key = api_key
        self._api_secret = api_secret
        self._environment = environment
        # Host según entorno: LIVE → producción real, TESTNET/PAPER → VST (demo).
        self._base_url = _BASE_URL_BY_ENV[environment]
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(10.0))
        # clientOrderId → symbol; poblado por get_open_orders para optimizar get_order_status.
        self._order_symbol_cache: dict[str, str] = {}
        # ONE_WAY es global a la cuenta; ISOLATED se fuerza por símbolo. Ambos se
        # verifican/fuerzan una única vez por instancia (ver _ensure_one_way_mode
        # y _ensure_isolated_margin), no en cada orden.
        self._one_way_verified = False
        self._isolated_verified_symbols: set[str] = set()
        # Circuit breaker de transporte, compartido por todas las llamadas firmadas de
        # esta instancia (F16). Ver `is_retryable_bingx_error` sobre qué cuenta como fallo.
        self._circuit_breaker = CircuitBreaker()

    @property
    def environment(self) -> Environment:
        return self._environment

    # ------------------------------------------------------------------
    # Read methods — tarjeta [98]
    # ------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        data: dict[str, Any] = self._signed_get("/openApi/swap/v2/user/balance", {})
        bal = data["balance"]
        return AccountState(
            balance_usdt=Decimal(str(bal["balance"])),
            equity_usdt=Decimal(str(bal["equity"])),
            available_margin_usdt=Decimal(str(bal["availableMargin"])),
            used_margin_usdt=Decimal(str(bal["usedMargin"])),
            is_simulated=False,
        )

    def get_position(self, symbol: str) -> PositionState | None:
        positions: list[dict[str, Any]] = self._signed_get(
            "/openApi/swap/v2/user/positions",
            {"symbol": _to_bingx_symbol(symbol)},
        )
        if not positions:
            return None
        pos = positions[0]
        amt = Decimal(str(pos["positionAmt"]))
        if amt == Decimal("0"):
            return None
        side = OrderSide.BUY if amt > 0 else OrderSide.SELL
        return PositionState(
            symbol=symbol,
            side=side,
            quantity=abs(amt),
            entry_price=Decimal(str(pos["avgPrice"])),
            mark_price=Decimal(str(pos["markPrice"])),
            unrealized_pnl=Decimal(str(pos["unrealizedProfit"])),
            margin_usdt=Decimal(str(pos["initialMargin"])),
            leverage=int(pos["leverage"]),
            is_simulated=False,
        )

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        data: dict[str, Any] = self._signed_get(
            "/openApi/swap/v2/trade/openOrders",
            {"symbol": _to_bingx_symbol(symbol)},
        )
        results: list[OrderResult] = []
        for raw in data.get("orders", []):
            client_oid: str = raw.get("clientOrderId", "")
            if client_oid:
                self._order_symbol_cache[client_oid] = symbol
            results.append(self._parse_order(raw, symbol))
        return results

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        # Intentar primero con el símbolo en caché, luego el resto.
        cached = self._order_symbol_cache.get(client_order_id)
        candidates: list[str] = [cached] if cached else []
        candidates += [s for s in sorted(ALLOWED_SYMBOLS) if s not in candidates]

        for symbol in candidates:
            try:
                data: dict[str, Any] = self._signed_get(
                    "/openApi/swap/v2/trade/order",
                    {
                        "symbol": _to_bingx_symbol(symbol),
                        "clientOrderID": client_order_id,
                    },
                )
                order = _extract_order(data)
                if order:
                    self._order_symbol_cache[client_order_id] = symbol
                    return self._parse_order(order, symbol)
            except BingXApiError as exc:
                _log.debug(
                    "get_order_status: order not found for symbol, trying next",
                    symbol=symbol,
                    client_order_id=client_order_id,
                    error=str(exc),
                )
            except httpx.HTTPStatusError as exc:
                _log.warning(
                    "get_order_status: HTTP error probing symbol, trying next",
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status_code=exc.response.status_code,
                )
        return None

    # ------------------------------------------------------------------
    # Order methods — tarjeta [99]
    # ------------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._ensure_one_way_mode()
        self._ensure_isolated_margin(request.symbol)

        # No es atómico con el POST de abajo: asume que el caller (Execution Engine)
        # serializa place_order por client_order_id, igual que exige la idempotencia
        # documentada en base.py. BingX también rechaza clientOrderID duplicado.
        existing = self._query_order(request.symbol, request.client_order_id)
        if existing is not None:
            _log.info(
                "bingx_adapter.idempotent_order",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
            )
            return existing

        params: dict[str, str] = {
            "symbol": _to_bingx_symbol(request.symbol),
            "type": request.order_type.value,
            "side": request.side.value,
            "positionSide": "BOTH",  # ONE_WAY mode obligatorio.
            "quantity": str(request.quantity),
            "clientOrderID": request.client_order_id,
            "reduceOnly": "true" if request.is_reduce_only else "false",
        }
        if request.price is not None:
            params["price"] = str(request.price)
        if request.stop_price is not None:
            params["stopPrice"] = str(request.stop_price)

        data: dict[str, Any] = self._signed_request("POST", "/openApi/swap/v2/trade/order", params)
        # Sin fallback: si BingX no devuelve "order", queremos un KeyError claro acá
        # en vez de un fallo opaco más adelante en _parse_order.
        raw_order: dict[str, Any] = data["order"]
        result = self._parse_order(raw_order, request.symbol)
        self._order_symbol_cache[result.client_order_id] = request.symbol
        _log.info(
            "bingx_adapter.order_placed",
            client_order_id=result.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status=result.status,
        )
        return result

    def cancel_order(self, client_order_id: str) -> bool:
        existing = self.get_order_status(client_order_id)
        if existing is None or existing.status not in (
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
        ):
            return False

        try:
            self._signed_request(
                "DELETE",
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": _to_bingx_symbol(existing.symbol),
                    "clientOrderID": client_order_id,
                },
            )
        except BingXApiError as exc:
            _log.warning(
                "bingx_adapter.cancel_order_failed",
                client_order_id=client_order_id,
                error=str(exc),
            )
            return False

        _log.info("bingx_adapter.order_cancelled", client_order_id=client_order_id)
        return True

    def set_leverage(self, symbol: str, leverage: int) -> None:
        cap = _MAX_LEVERAGE_BY_ENV[self._environment]
        if leverage < 1 or leverage > cap:
            raise ValueError(
                f"Leverage {leverage}x fuera del rango permitido en {self._environment} (1–{cap}x)"
            )
        self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            {
                "symbol": _to_bingx_symbol(symbol),
                "side": "BOTH",
                "leverage": str(leverage),
            },
        )
        _log.info("bingx_adapter.leverage_set", symbol=symbol, leverage=leverage)

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        if margin_type == MarginType.CROSS:
            raise ValueError("Cross margin está prohibido. Solo se permite ISOLATED.")
        self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {
                "symbol": _to_bingx_symbol(symbol),
                "marginType": margin_type.value,
            },
        )
        _log.info("bingx_adapter.margin_type_set", symbol=symbol, margin_type=margin_type)

    # ------------------------------------------------------------------
    # HTTP / firma
    # ------------------------------------------------------------------

    def _ensure_one_way_mode(self) -> None:
        """Fuerza dualSidePosition=false (ONE_WAY). Una sola vez por instancia (§9.4 docs).

        Endpoint bajo /swap/v1/ (no /v2/trade/): confirmado contra la API real de
        BingX y contra la implementación de ccxt (github.com/ccxt/ccxt), que expone
        este endpoint como swap.v1.private.{get,post}['positionSide/dual'], sin
        segmento "/trade/". El path /v2/trade/positionSide/dual documentado
        originalmente en docs/bingx_api_reference.md no existe (BingX responde
        "code: 100400, this api is not exist").
        """
        if self._one_way_verified:
            return
        try:
            self._signed_request(
                "POST",
                "/openApi/swap/v1/positionSide/dual",
                {"dualSidePosition": "false"},
            )
        except BingXApiError as exc:
            _log.warning("bingx_adapter.ensure_one_way_failed", error=str(exc))
            raise
        self._one_way_verified = True
        _log.info("bingx_adapter.one_way_mode_forced")

    def _ensure_isolated_margin(self, symbol: str) -> None:
        """Fuerza marginType=ISOLATED para symbol. Una sola vez por símbolo (§9.4 docs)."""
        if symbol in self._isolated_verified_symbols:
            return
        try:
            self.set_margin_type(symbol, MarginType.ISOLATED)
        except BingXApiError as exc:
            _log.warning("bingx_adapter.ensure_isolated_failed", symbol=symbol, error=str(exc))
            raise
        self._isolated_verified_symbols.add(symbol)

    def _query_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        """Consulta puntual de una orden por symbol+client_order_id. None si no existe.

        A diferencia de get_order_status, no atrapa httpx.HTTPStatusError: un error
        HTTP transitorio acá debe abortar place_order (fail-closed) en vez de
        arriesgar un POST duplicado con información incompleta.
        """
        try:
            data: dict[str, Any] = self._signed_get(
                "/openApi/swap/v2/trade/order",
                {"symbol": _to_bingx_symbol(symbol), "clientOrderID": client_order_id},
            )
        except BingXApiError:
            # Cualquier error de API (incluido auth/rate-limit, no solo "order not
            # found") se trata como "no existe". Si el error era transitorio y la
            # orden sí existía, el POST siguiente en place_order falla con el mismo
            # error — no hay riesgo de duplicado silencioso.
            return None
        order = _extract_order(data)
        if not order:
            return None
        self._order_symbol_cache[client_order_id] = symbol
        return self._parse_order(order, symbol)

    def _signed_get(self, path: str, params: dict[str, str]) -> Any:
        return self._signed_request("GET", path, params)

    def _signed_request(self, method: str, path: str, params: dict[str, str]) -> Any:
        """Request autenticado: añade timestamp y firma HMAC-SHA256 al query string.

        BingX Futures espera los parámetros en el query string para todos los
        métodos (GET/POST/DELETE), no en el body. Los valores se URL-encodean
        antes de firmar para que la firma coincida siempre con lo que se envía.

        Reintenta con backoff exponencial + jitter (F16) solo ante fallos de
        transporte (sin respuesta, timeout, 5xx) — ver `is_retryable_bingx_error`.
        Errores de negocio (`code != 0` → BingXApiError, o 4xx) se propagan de
        inmediato, sin reintento, igual que antes de F16.
        """

        def _do_request() -> Any:
            ts = str(int(time.time() * 1000))
            query_parts = [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items()]
            query_parts.append(f"timestamp={ts}")
            query_string = "&".join(query_parts)
            signature = hmac.new(
                self._api_secret.encode(),
                query_string.encode(),
                hashlib.sha256,
            ).hexdigest()
            url = f"{self._base_url}{path}?{query_string}&signature={signature}"
            response = self._http.request(method, url, headers={"X-BX-APIKEY": self._api_key})
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            if body.get("code", -1) != 0:
                _log.warning(
                    "BingX API returned non-zero code",
                    path=path,
                    code=body.get("code"),
                    msg=body.get("msg", ""),
                )
                raise BingXApiError(f"BingX error {body.get('code')}: {body.get('msg', '')}")
            return body["data"]

        def _on_retry(attempt: int, delay: float, exc: Exception) -> None:
            _log.info(
                "bingx_adapter.retry",
                attempt=attempt,
                delay_seconds=delay,
                path=path,
                error=str(exc),
            )

        try:
            return retry_sync(
                _do_request,
                config=_RETRY_CONFIG,
                is_retryable=is_retryable_bingx_error,
                circuit_breaker=self._circuit_breaker,
                on_retry=_on_retry,
            )
        except CircuitBreakerOpenError as exc:
            raise BingXApiError(str(exc)) from exc

    def _parse_order(self, raw: dict[str, Any], symbol: str) -> OrderResult:
        """Mapea un dict de orden BingX → OrderResult."""
        status = _STATUS_MAP.get(raw.get("status", "FAILED"), OrderStatus.FAILED)
        raw_type = raw.get("type", "MARKET")
        order_type = _ORDER_TYPE_MAP.get(raw_type)
        if order_type is None:
            _log.warning(
                "unknown BingX order type, defaulting to MARKET",
                raw_type=raw_type,
                order_id=raw.get("orderId"),
            )
            order_type = OrderType.MARKET

        avg_price_str: str = raw.get("avgPrice") or raw.get("price") or "0"
        fill_price = Decimal(avg_price_str) if Decimal(avg_price_str) > 0 else None

        ts_ms: int = int(raw.get("time") or raw.get("updateTime") or 0)
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms else datetime.now(UTC)

        # BingX devuelve el campo como "clientOrderID" (ID mayúscula) en la respuesta
        # real de POST /trade/order, no "clientOrderId". Se leen ambas casings — igual
        # que ccxt (safe_string_n(['clientOrderID', 'clientOrderId', ...])) — para no
        # perder el id de idempotencia. Detectado en la tarjeta [101] contra el host VST.
        client_order_id = raw.get("clientOrderID") or raw.get("clientOrderId") or ""

        return OrderResult(
            order_id=str(raw.get("orderId", "")),
            client_order_id=str(client_order_id),
            symbol=symbol,
            side=OrderSide(raw["side"]),
            order_type=order_type,
            status=status,
            quantity_requested=Decimal(str(raw.get("origQty", "0"))),
            quantity_filled=Decimal(str(raw.get("executedQty", "0"))),
            fill_price=fill_price,
            fee_usdt=Decimal(str(raw.get("fee", "0") or "0")),
            slippage_usdt=Decimal("0"),
            is_simulated=False,
            timestamp_utc=ts,
        )
