"""Toolkit de inyección de fallos para la suite de caos (F16 [118]).

Envuelve las dos dependencias externas del bot — el `ExchangeAdapter` y el
`DataFetcher` — para inyectar los cuatro tipos de fallo del DoD: timeouts,
respuestas 5xx, desconexiones y datos corruptos. Nada acá simula lógica de
negocio: cada wrapper delega en el objeto real salvo cuando una regla de fallo
activa lo intercepta.

Reglas:
- `fail(method, ...)`  → la llamada levanta una excepción (desconexión / 5xx).
- `hang(method, ...)`  → la llamada duerme N segundos antes de delegar (deja que
  el timeout del caller dispare).
- `corrupt(method, fn, ...)` → la llamada delega y después pasa el resultado por
  `fn` (datos corruptos: status inválido, cantidad cambiada, etc.).

Cada regla lleva un contador `times`: con `times=N` el fallo se aplica las
primeras N llamadas y después el wrapper se recupera solo; con `times=None`
(default) el fallo es permanente. Las reglas pueden acotarse a un `symbol`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    PositionState,
)
from backend.market_data.fetcher import DataFetcher
from backend.market_data.schemas import MarketSnapshot


class InjectedDisconnectError(ConnectionError):
    """Pérdida de conexión simulada con el exchange (socket / DNS / red caída)."""


class InjectedServerError(RuntimeError):
    """Respuesta 5xx simulada del exchange."""


class InjectedTimeout(TimeoutError):
    """Timeout de transporte simulado (se agotó el deadline sin respuesta)."""


@dataclass
class _Rule:
    """Regla de fallo consumible."""

    remaining: int | None  # None => permanente
    exc: BaseException | None = None
    hang_seconds: float | None = None
    corrupt: Callable[[object], object] | None = None

    def consume(self) -> bool:
        """True si la regla debe aplicarse a esta llamada (y descuenta el contador)."""
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


_RuleKey = tuple[str, str | None]


class ChaosAdapter(ExchangeAdapter):
    """Envuelve un `ExchangeAdapter` real e inyecta fallos por método y símbolo."""

    def __init__(self, wrapped: ExchangeAdapter) -> None:
        self._wrapped = wrapped
        self._rules: dict[_RuleKey, _Rule] = {}
        self.call_count: dict[str, int] = {}

    # -- configuración --------------------------------------------------

    def fail(
        self,
        method: str,
        *,
        exc: BaseException | None = None,
        symbol: str | None = None,
        times: int | None = None,
    ) -> None:
        self._rules[method, symbol] = _Rule(
            remaining=times,
            exc=exc or InjectedDisconnectError(f"chaos: {method} desconectado"),
        )

    def hang(
        self,
        method: str,
        *,
        seconds: float,
        symbol: str | None = None,
        times: int | None = None,
    ) -> None:
        self._rules[method, symbol] = _Rule(remaining=times, hang_seconds=seconds)

    def corrupt(
        self,
        method: str,
        transform: Callable[[object], object],
        *,
        symbol: str | None = None,
        times: int | None = None,
    ) -> None:
        self._rules[method, symbol] = _Rule(remaining=times, corrupt=transform)

    def vanish_position(self, symbol: str, *, times: int | None = None) -> None:
        """El exchange deja de reportar la posición de `symbol` (se ve flat)."""
        self.corrupt("get_position", lambda _pos: None, symbol=symbol, times=times)

    # -- motor de reglas ---------------------------------------------------

    def _rule_for(self, method: str, symbol: str | None) -> _Rule | None:
        if symbol is not None and (method, symbol) in self._rules:
            return self._rules[method, symbol]
        return self._rules.get((method, None))

    def _apply[T](self, method: str, symbol: str | None, call: Callable[[], T]) -> T:
        self.call_count[method] = self.call_count.get(method, 0) + 1
        rule = self._rule_for(method, symbol)
        if rule is not None and rule.consume():
            if rule.hang_seconds is not None:
                time.sleep(rule.hang_seconds)
            if rule.exc is not None:
                raise rule.exc
            result = call()
            if rule.corrupt is not None:
                return rule.corrupt(result)  # type: ignore[return-value]
            return result
        return call()

    # -- ExchangeAdapter --------------------------------------------------

    @property
    def environment(self) -> Environment:
        return self._wrapped.environment

    def place_order(self, request: OrderRequest) -> OrderResult:
        return self._apply(
            "place_order", request.symbol, lambda: self._wrapped.place_order(request)
        )

    def cancel_order(self, client_order_id: str) -> bool:
        return self._apply(
            "cancel_order", None, lambda: self._wrapped.cancel_order(client_order_id)
        )

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        return self._apply(
            "get_order_status", None, lambda: self._wrapped.get_order_status(client_order_id)
        )

    def get_position(self, symbol: str) -> PositionState | None:
        return self._apply("get_position", symbol, lambda: self._wrapped.get_position(symbol))

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        return self._apply("get_open_orders", symbol, lambda: self._wrapped.get_open_orders(symbol))

    def get_account_state(self) -> AccountState:
        return self._apply("get_account_state", None, self._wrapped.get_account_state)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._wrapped.set_leverage(symbol, leverage)

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        self._wrapped.set_margin_type(symbol, margin_type)


class ChaosFetcher(DataFetcher):
    """Envuelve un `DataFetcher` real e inyecta fallos / mutaciones por símbolo.

    `fail_symbol` levanta en `fetch_snapshot` (desconexión / 5xx del feed).
    `mutate_symbol` deja pasar el snapshot real y le aplica overrides vía
    `model_copy(update=...)` — sin re-validar, así se pueden inyectar valores
    fuera de rango (latencia/skew por encima del umbral, incoherencia bid/ask,
    timestamp stale) que el pipeline downstream debe rechazar o accionar.
    """

    def __init__(self, wrapped: DataFetcher) -> None:
        self._wrapped = wrapped
        self._rules: dict[str, _Rule] = {}
        self._overrides: dict[str, dict[str, object]] = {}

    def fail_symbol(
        self,
        symbol: str,
        *,
        exc: BaseException | None = None,
        times: int | None = None,
    ) -> None:
        self._rules[symbol] = _Rule(
            remaining=times,
            exc=exc or InjectedDisconnectError(f"chaos: feed de {symbol} caído"),
        )

    def mutate_symbol(self, symbol: str, **overrides: object) -> None:
        self._overrides.setdefault(symbol, {}).update(overrides)

    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: object,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        rule = self._rules.get(symbol)
        if rule is not None and rule.consume():
            assert rule.exc is not None
            raise rule.exc
        snapshot = await self._wrapped.fetch_snapshot(
            symbol,
            account_balance_usdt,  # type: ignore[arg-type]
            open_positions_count=open_positions_count,
            active_orders_count=active_orders_count,
        )
        overrides = self._overrides.get(symbol)
        if overrides:
            return snapshot.model_copy(update=overrides)
        return snapshot

    def is_healthy(self) -> bool:
        return self._wrapped.is_healthy()


__all__ = [
    "ChaosAdapter",
    "ChaosFetcher",
    "InjectedDisconnectError",
    "InjectedServerError",
    "InjectedTimeout",
]
