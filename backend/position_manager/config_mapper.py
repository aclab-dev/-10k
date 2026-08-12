"""Mapea la decisión aprobada + defaults de config.yaml a un PositionConfig (F14).

Alcance: SL/TP (passthrough), trailing stop (vía PositionManagementConfig.
trailing_default_*) y break-even, tanto la distancia de disparo (be_trigger_delta,
ATR-based) como el buffer de fees (be_sl_offset, percent-of-entry-based).
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from backend.core.config import PositionManagementConfig
from backend.position_manager.schemas import PositionConfig, TrailingMode

_log = structlog.get_logger(__name__)

# Tope de be_sl_offset como fracción de be_trigger_delta. PositionConfig exige
# be_sl_offset < be_trigger_delta (si no, el SL queda al nivel del trigger y cierra
# la posición en el mismo tick que activa el break-even); clampear acá evita que un
# entry_price alto combinado con un ATR relativo bajo viole esa invariante y haga
# explotar la apertura de la posición.
_BE_SL_OFFSET_MAX_FRACTION_OF_TRIGGER = Decimal("0.5")


def build_position_config(
    symbol: str,
    *,
    stop_loss: Decimal,
    take_profit: Decimal,
    use_trailing_stop: bool,
    move_to_break_even: bool,
    defaults: PositionManagementConfig,
    entry_price: Decimal,
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
            be_trigger_atr_multiplier, be_sl_offset_percent).
        entry_price: precio de entrada de la posición (fill real). Usado para
            calcular be_sl_offset como porcentaje del entry cuando el break-even
            queda activo.
        atr_1h: ATR de referencia (1h) al momento de abrir la posición. Requerido
            si el trailing efectivo queda en modo ATR o si el break-even queda activo.

    Nota: en régimen de baja volatilidad relativa (atr_1h/entry_price bajo),
        be_sl_offset se clampea a _BE_SL_OFFSET_MAX_FRACTION_OF_TRIGGER *
        be_trigger_delta para no violar la invariante be_sl_offset < be_trigger_delta.
        El offset final en ese caso puede quedar por debajo del costo redondo de fees
        que be_sl_offset_percent está pensado para cubrir — no está garantizado que
        cubra el round-trip. El clamp deja rastro en el log
        (position_manager.be_sl_offset_clamped).

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
    be_sl_offset = Decimal("0")

    if move_to_break_even and defaults.break_even_enabled:
        if atr_1h is None:
            raise ValueError(
                "move_to_break_even requiere atr_1h para calcular be_trigger_delta "
                "(atr_1h * be_trigger_atr_multiplier), pero no se proveyó ninguno."
            )
        be_trigger_delta = atr_1h * Decimal(str(defaults.be_trigger_atr_multiplier))

        fee_buffer = entry_price * Decimal(str(defaults.be_sl_offset_percent))
        max_offset = be_trigger_delta * _BE_SL_OFFSET_MAX_FRACTION_OF_TRIGGER
        be_sl_offset = min(fee_buffer, max_offset)

        if fee_buffer > max_offset:
            _log.warning(
                "position_manager.be_sl_offset_clamped",
                symbol=symbol,
                entry_price=str(entry_price),
                atr_1h=str(atr_1h),
                fee_buffer=str(fee_buffer),
                be_trigger_delta=str(be_trigger_delta),
                be_sl_offset=str(be_sl_offset),
            )

    return PositionConfig(
        symbol=symbol,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_percent=trailing_percent,
        trailing_atr=trailing_atr,
        trailing_atr_multiplier=trailing_atr_multiplier,
        be_trigger_delta=be_trigger_delta,
        be_sl_offset=be_sl_offset,
    )
