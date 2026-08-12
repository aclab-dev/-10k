"""ExchangeAdapter — interfaz abstracta para operaciones de exchange.

El Execution Engine y el Market Data Engine interactúan con el exchange
exclusivamente a través de esta interfaz. Esto permite intercambiar
PaperAdapter, BingXAdapter o BinanceAdapter sin tocar la lógica de negocio.

Regla no negociable: nada del Trading Core puede acoplarse a endpoints
concretos de BingX o Binance. Todo acceso al exchange pasa por aquí.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    PositionState,
)


class ExchangeAdapter(ABC):
    """Contrato que todo adapter de exchange debe implementar."""

    @property
    @abstractmethod
    def environment(self) -> Environment:
        """Entorno al que pertenece este adapter (PAPER / TESTNET / LIVE)."""

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        """Coloca o simula una orden en el exchange.

        Si ya existe una orden con el mismo client_order_id, retorna el
        resultado previo sin crear duplicados (idempotencia obligatoria).
        """

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> bool:
        """Cancela una orden pendiente.

        Returns:
            True si la orden fue cancelada. False si ya estaba filled,
            no existía o no pudo cancelarse.
        """

    @abstractmethod
    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        """Consulta el estado actual de una orden por su client_order_id.

        Returns:
            OrderResult si la orden existe, None si no se encuentra.
        """

    @abstractmethod
    def get_position(self, symbol: str) -> PositionState | None:
        """Retorna la posición abierta para el símbolo dado, o None si no hay.

        Contrato: None significa "no hay posición", nunca "no pude averiguarlo".
        Ante error de API, red o timeout la implementación DEBE levantar excepción,
        no retornar None. PositionManager.tick() interpreta None como cierre externo
        y descarta la PositionConfig del símbolo.
        """

    @abstractmethod
    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        """Retorna todas las órdenes abiertas (PENDING) para el símbolo dado."""

    @abstractmethod
    def get_account_state(self) -> AccountState:
        """Retorna el estado actual de la cuenta (balance, equity, márgenes)."""

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Configura el leverage para el símbolo dado.

        Raises:
            ValueError: si el leverage supera el cap del entorno.
        """

    @abstractmethod
    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        """Configura el tipo de margen para el símbolo dado.

        Raises:
            ValueError: si margin_type es CROSS (cross margin está prohibido).
        """
