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

    Aislamiento de fallas: un símbolo que falla (ej. get_mark_price cuelga o
    tira) se loguea como ERROR y se saltea; el resto de los símbolos configurados
    igual se tickean en ese ciclo. PositionManager vive 100% en memoria — si un
    fallo de un solo símbolo tirara toda la excepción hacia arriba, mataría el
    loop operativo entero; un restart del proceso pierde todos los PositionConfig
    y deja el resto de las posiciones abiertas sin ningún monitoreo de SL/TP, en
    silencio. Aislar por símbolo es más seguro que "fail loud" a nivel de ciclo.

    Contrato de get_mark_price: debe imponer su propio timeout. tick_all() llama
    a get_mark_price mientras sostiene el lock; una llamada que cuelga bloquea
    todo el ciclo (y por lo tanto el resto de los símbolos) indefinidamente.
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
        """Tickea cada símbolo con config activa. Retorna un TickResult por símbolo

        tickeado exitosamente (los símbolos que fallan se omiten del resultado,
        no interrumpen el resto del ciclo — ver nota de aislamiento de fallas).
        """
        with self._lock:
            symbols = self._pm.configured_symbols()
            results: list[TickResult] = []

            for symbol in symbols:
                try:
                    mark_price = self._get_mark_price(symbol)
                    results.append(self._pm.tick(symbol, mark_price))
                except Exception:
                    _log.error("position_tick_service.tick_failed", symbol=symbol, exc_info=True)

            if symbols:
                triggers = [
                    r.trigger.value for r in results if r.trigger != PositionTriggerReason.NONE
                ]
                _log.info(
                    "position_tick_service.cycle",
                    symbols_ticked=len(symbols),
                    symbols_succeeded=len(results),
                    triggers=triggers,
                )

            return results
