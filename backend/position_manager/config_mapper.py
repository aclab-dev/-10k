"""Mapea la decisión aprobada + defaults de config.yaml a un PositionConfig (F14).

Alcance: SL/TP (passthrough), trailing stop (vía PositionManagementConfig.
trailing_default_*) y break-even (vía be_trigger_atr_multiplier). Ambos mecanismos
de gestión son ATR-based y consumen el mismo atr_1h snapshoteado al abrir.

be_sl_offset queda en 0 (SL exactamente en entry): config.yaml todavía no define un
buffer default para cubrir fees. Es una tarjeta aparte, no un olvido.
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
    move_to_break_even: bool,
    defaults: PositionManagementConfig,
    atr_1h: Decimal | None = None,
) -> PositionConfig:
    """Arma el PositionConfig de salida al abrir una posición.

    Args:
        symbol: símbolo de la posición.
        stop_loss: SL de la decisión aprobada (pasa directo, sin ajustes).
        take_profit: TP de la decisión aprobada (pasa directo, sin ajustes).
        use_trailing_stop: PositionManagementPlan.use_trailing_stop de la decisión.
        move_to_break_even: PositionManagementPlan.move_to_break_even de la decisión.
        defaults: sección position_management de config.yaml (trailing_default_*,
            be_trigger_atr_multiplier).
        atr_1h: ATR de referencia (1h) al momento de abrir la posición. Requerido
            si el trailing efectivo queda en modo ATR o si el break-even queda activo.

    Raises:
        ValueError: si el trailing o el break-even efectivos requieren un dato que no
            está disponible (trailing FIXED sin default en config.yaml, o cualquiera
            de los dos mecanismos ATR-based sin atr_1h).
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

    be_trigger_delta: Decimal | None = None

    if move_to_break_even and defaults.break_even_enabled:
        if atr_1h is None:
            raise ValueError(
                "move_to_break_even requiere atr_1h para calcular be_trigger_delta "
                "(atr_1h * be_trigger_atr_multiplier), pero no se proveyó ninguno."
            )
        be_trigger_delta = atr_1h * Decimal(str(defaults.be_trigger_atr_multiplier))

    return PositionConfig(
        symbol=symbol,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_percent=trailing_percent,
        trailing_atr=trailing_atr,
        trailing_atr_multiplier=trailing_atr_multiplier,
        be_trigger_delta=be_trigger_delta,
    )
