"""PositionTickService — consumidor del PositionManager (F14).

Le pasa mark_price por símbolo en cada ciclo del loop operativo, cerrando el
gap descripto en manager.py: "el caller es responsable de los ticks periódicos".
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from decimal import Decimal

import structlog

from backend.position_manager.manager import PositionManager
from backend.position_manager.schemas import PositionTriggerReason, TickResult

_log = structlog.get_logger(__name__)


class PositionTickService:
    """Tickea, de forma serializada, todos los símbolos con config activa.

    Nota de thread-safety: PositionManager.tick() no es seguro de llamar
    concurrentemente para el mismo símbolo (ver manager.py). tick_all() serializa
    todas las llamadas bajo un lock, no solo entre símbolos distintos dentro de
    una misma invocación sino también entre invocaciones concurrentes de
    tick_all() (por ejemplo si el loop operativo se dispara dos veces solapadas).
    """

    def __init__(
        self,
        position_manager: PositionManager,
        get_mark_price: Callable[[str], Decimal],
    ) -> None:
        self._pm = position_manager
        self._get_mark_price = get_mark_price
        self._lock = threading.Lock()

    def tick_all(self) -> list[TickResult]:
        """Tickea cada símbolo con config activa. Retorna un TickResult por símbolo."""
        with self._lock:
            symbols = self._pm.configured_symbols()
            results = [self._pm.tick(symbol, self._get_mark_price(symbol)) for symbol in symbols]

            if symbols:
                triggers = [
                    r.trigger.value for r in results if r.trigger != PositionTriggerReason.NONE
                ]
                _log.info(
                    "position_tick_service.cycle",
                    symbols_ticked=len(symbols),
                    triggers=triggers,
                )

            return results
