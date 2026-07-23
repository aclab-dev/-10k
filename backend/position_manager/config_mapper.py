"""Mapea la decisión aprobada + defaults de config.yaml a un PositionConfig (F14).

Alcance: solo SL/TP (passthrough) y trailing stop (vía PositionManagementConfig.
trailing_default_*). Break-even queda fuera: config.yaml no define un valor default
de distancia (be_trigger_delta), así que no hay nada determinístico que mapear todavía.
"""

from __future__ import annotations

from decimal import Decimal

from backend.core.config import PositionManagementConfig
from backend.position_manager.schemas import PositionConfig, TrailingMode


def build_position_config(
    symbol: str,
    stop_loss: Decimal,
    take_profit: Decimal,
    use_trailing_stop: bool,
    defaults: PositionManagementConfig,
    atr_1h: Decimal | None = None,
) -> PositionConfig:
    """Arma el PositionConfig de salida al abrir una posición.

    Args:
        symbol: símbolo de la posición.
        stop_loss: SL de la decisión aprobada (pasa directo, sin ajustes).
        take_profit: TP de la decisión aprobada (pasa directo, sin ajustes).
        use_trailing_stop: PositionManagementPlan.use_trailing_stop de la decisión.
        defaults: sección position_management de config.yaml (trailing_default_*).
        atr_1h: ATR de referencia (1h) al momento de abrir la posición. Requerido
            solo si el trailing efectivo queda en modo ATR.

    Raises:
        ValueError: si el trailing efectivo requiere un dato que no está disponible
            (modo FIXED sin default en config.yaml, o modo ATR sin atr_1h).
    """
    trailing_percent: Decimal | None = None
    trailing_atr: Decimal | None = None
    trailing_atr_multiplier: Decimal | None = None

    if use_trailing_stop and defaults.trailing_stop_enabled:
        mode = TrailingMode(defaults.trailing_default_mode)

        if mode is TrailingMode.FIXED:
            raise ValueError(
                "trailing_default_mode=FIXED no tiene un valor default de distancia en "
                "config.yaml (solo trailing_default_percent/trailing_default_atr_multiplier "
                "están definidos). No soportado por build_position_config."
            )
        if mode is TrailingMode.PERCENT:
            trailing_percent = Decimal(str(defaults.trailing_default_percent))
        elif mode is TrailingMode.ATR:
            if atr_1h is None:
                raise ValueError(
                    "trailing_default_mode=ATR requiere atr_1h para snapshotear "
                    "PositionConfig.trailing_atr, pero no se proveyó ninguno."
                )
            trailing_atr = atr_1h
            trailing_atr_multiplier = Decimal(str(defaults.trailing_default_atr_multiplier))

    return PositionConfig(
        symbol=symbol,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_percent=trailing_percent,
        trailing_atr=trailing_atr,
        trailing_atr_multiplier=trailing_atr_multiplier,
    )
