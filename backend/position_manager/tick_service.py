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

    Aislamiento de fallas: un símbolo que falla se loguea como ERROR y se
    saltea; el resto de los símbolos configurados igual se tickean en ese ciclo.
    PositionManager vive 100% en memoria — si un fallo de un solo símbolo tirara
    toda la excepción hacia arriba, mataría el loop operativo entero; un restart
    del proceso pierde todos los PositionConfig y deja el resto de las posiciones
    abiertas sin ningún monitoreo de SL/TP, en silencio. Aislar por símbolo es
    más seguro que "fail loud" a nivel de ciclo.

    Los dos puntos de falla se tratan distinto:
    - get_mark_price falla: el símbolo se saltea, su config queda intacta
      (nunca se llamó a pm.tick), se reintenta solo en el próximo ciclo.
    - pm.tick() falla: la re-registración es condicional a que el símbolo haya
      quedado realmente huérfano (get_config(symbol) is None tras la falla).
      * Cierre total (SL, TP single, último nivel de multi-TP, trailing,
        invalidación con close_fraction=1): PositionManager ya hizo
        remove_config() en su finally antes de que la excepción suba (ver
        manager.py) — la posición queda abierta y sin config. Se re-registra
        la config previa a la falla, tal como indica el docstring de tick():
        "el caller debe capturar y llamar set_config para reintentar". Nota:
        set_config() resetea el SL/TP efectivo y el high-water del trailing a
        los valores originales — se pierde cualquier progreso de break-even o
        ajuste dinámico previo, pero la posición no queda descubierta.
      * Nivel parcial de multi-TP: el manager NO borra la config en su finally
        (solo lo hace si es el último nivel) — preserva _remaining_tp_levels a
        propósito para que el próximo tick reintente ese nivel puntual. Acá NO
        hay que re-registrar: PositionConfig es inmutable y siempre trae la
        lista completa de niveles originales, así que set_config() resetearía
        _remaining_tp_levels a esa lista completa, resucitando niveles ya
        cerrados — el próximo tick los volvería a cerrar (sobre-cierre).
      * Invalidación con cierre parcial o solo ajuste de SL (F14): análogo al
        nivel parcial de multi-TP — la config tampoco se borra en su finally,
        y el guard interno _auto_invalidation_fired solo se marca si la
        acción se aplicó sin excepción (ver manager.py), así que el próximo
        tick reintenta la invalidación completa. Tampoco hay que re-registrar
        acá.

    Contrato de get_mark_price: debe imponer su propio timeout. tick_all() llama
    a get_mark_price mientras sostiene el lock; una llamada que cuelga bloquea
    todo el ciclo (y por lo tanto el resto de los símbolos) indefinidamente —
    el try/except de acá no ayuda en ese caso, solo atrapa excepciones, no hangs.
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
        """Tickea cada símbolo con config activa.

        Retorna un TickResult por símbolo tickeado exitosamente. Los símbolos
        que fallan se omiten del resultado, pero no interrumpen el resto del
        ciclo — ver nota de aislamiento de fallas.
        """
        with self._lock:
            symbols = self._pm.configured_symbols()
            results: list[TickResult] = []

            for symbol in symbols:
                try:
                    mark_price = self._get_mark_price(symbol)
                except Exception:
                    _log.error(
                        "position_tick_service.mark_price_failed", symbol=symbol, exc_info=True
                    )
                    continue

                config_before_tick = self._pm.get_config(symbol)
                try:
                    results.append(self._pm.tick(symbol, mark_price))
                except Exception:
                    _log.error(
                        "position_tick_service.tick_failed",
                        symbol=symbol,
                        exc_info=True,
                    )
                    # Re-registrar solo si tick() de verdad dejó el símbolo huérfano
                    # (remove_config ya corrió en su finally: cierre total — SL, TP
                    # single, último nivel de multi-TP, trailing). Si la config sigue
                    # presente, es un nivel *parcial* de multi-TP que falló: el manager
                    # ya preservó su estado a propósito (_remaining_tp_levels intacto,
                    # sin popear el nivel). Re-registrar acá pisaría ese estado con la
                    # config original completa, resucitando niveles ya cerrados y
                    # sobre-cerrando la posición en el próximo tick.
                    if config_before_tick is not None and self._pm.get_config(symbol) is None:
                        self._pm.set_config(config_before_tick)
                        _log.error(
                            "position_tick_service.config_reregistered_after_failure",
                            symbol=symbol,
                        )

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
