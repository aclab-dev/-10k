"""Modelo de latencia para simulación de ejecución en backtesting.

En un exchange real hay un retardo entre la decisión y el fill:
  - Latencia de red, procesamiento, queue de órdenes, etc.

En backtesting el fill base ya se retrasa 1 candle (señal en N → fill en N+1 open).
Este modelo agrega candles adicionales de latencia sobre ese retardo base.

El modelo es intencionalmente simple: latencia fija en candles. Modelos más
sofisticados (latencia variable, distribución probabilística) pueden reemplazarlo
sin cambiar la interfaz.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyModel:
    """Retardo adicional de ejecución medido en candles completos.

    Args:
        extra_candles: candles de retardo adicional sobre el retardo base de 1 candle.
                       0 → fill en el open del candle siguiente a la señal (base).
                       1 → fill en el open 2 candles después de la señal.
    """

    extra_candles: int = 0

    def __post_init__(self) -> None:
        if self.extra_candles < 0:
            raise ValueError(f"extra_candles no puede ser negativo; got {self.extra_candles}")

    def fill_candle_offset(self) -> int:
        """Devuelve el offset total desde la señal hasta el candle de fill.

        El offset base es 1 (señal en candle N → fill en candle N+1).
        """
        return 1 + self.extra_candles
