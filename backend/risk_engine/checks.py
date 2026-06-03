"""Validaciones individuales del Risk Engine — sección 4.10.9 del PDF maestro.

Cada función recibe los parámetros relevantes y devuelve un CheckResult
indicando si el check pasó (PASS), requiere ajuste (ADJUST_DOWN) o bloquea
el trade (BLOCK).

Las funciones son puras (sin side-effects) y testeables de forma aislada.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from backend.core.config import AppConfig, Environment, LiquidationSafetyConfig
from backend.decision_engine.schemas import DecisionType, ModelDecision

# ---------------------------------------------------------------------------
# Tipos de resultado
# ---------------------------------------------------------------------------


class CheckOutcome(Enum):
    PASS = auto()
    BLOCK = auto()
    ADJUST_DOWN = auto()


@dataclass(frozen=True)
class CheckResult:
    outcome: CheckOutcome
    rule: str
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def leverage_cap_for_env(config: AppConfig, environment: Environment) -> int:
    """Devuelve el tope de leverage para el entorno dado."""
    lev = config.leverage
    if environment == Environment.PAPER:
        return lev.max_leverage_paper
    if environment == Environment.TESTNET:
        return lev.max_leverage_testnet
    # LIVE: usa el cap absoluto; el cap inicial (≤3x) lo aplica el execution layer.
    return lev.max_leverage_live_absolute


# ---------------------------------------------------------------------------
# Checks BLOCK
# ---------------------------------------------------------------------------


def check_sl_required(decision: ModelDecision) -> CheckResult:
    """SL obligatorio cuando execute=True. Defensa en profundidad."""
    if decision.execute and decision.stop_loss <= 0:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="sl_required",
            reason="SL ausente o cero: stop_loss obligatorio cuando execute=True.",
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="sl_required",
        reason="SL presente.",
    )


def check_tp_or_exit_plan(decision: ModelDecision) -> CheckResult:
    """TP > 0 o plan de salida definido cuando execute=True."""
    if not decision.execute:
        return CheckResult(
            outcome=CheckOutcome.PASS,
            rule="tp_or_exit_plan",
            reason="No aplica: execute=False.",
        )
    has_tp = decision.take_profit > 0
    plan = decision.position_management_plan
    has_exit_plan = (
        plan.use_trailing_stop
        or plan.move_to_break_even
        or bool(plan.partial_close_plan.strip())
        or plan.max_time_in_trade_minutes > 0
    )
    if not has_tp and not has_exit_plan:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="tp_or_exit_plan",
            reason=(
                "Sin TP ni plan de salida: se requiere take_profit > 0 o al menos un "
                "elemento de position_management_plan cuando execute=True."
            ),
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="tp_or_exit_plan",
        reason="TP o plan de salida presente.",
    )


def check_daily_drawdown(
    daily_loss_usdt: Decimal,
    initial_balance_usdt: Decimal,
    max_daily_loss_percent: float,
) -> CheckResult:
    """Pérdida diaria < max_daily_loss_percent del balance inicial."""
    if initial_balance_usdt <= 0:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="daily_drawdown",
            reason="Balance inicial inválido (≤0): no se puede calcular drawdown diario.",
        )
    daily_pct = float(daily_loss_usdt / initial_balance_usdt) * 100
    if daily_pct >= max_daily_loss_percent:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="daily_drawdown",
            reason=(
                f"Drawdown diario {daily_pct:.2f}% alcanzó el límite de "
                f"{max_daily_loss_percent}%: no se permiten nuevas entradas."
            ),
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="daily_drawdown",
        reason=(
            f"Drawdown diario {daily_pct:.2f}% dentro del límite de {max_daily_loss_percent}%."
        ),
    )


def check_total_drawdown(
    total_loss_usdt: Decimal,
    initial_balance_usdt: Decimal,
    max_total_loss_percent: float,
) -> CheckResult:
    """Pérdida total < max_total_loss_percent del balance inicial."""
    if initial_balance_usdt <= 0:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="total_drawdown",
            reason="Balance inicial inválido (≤0): no se puede calcular drawdown total.",
        )
    total_pct = float(total_loss_usdt / initial_balance_usdt) * 100
    if total_pct >= max_total_loss_percent:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="total_drawdown",
            reason=(
                f"Drawdown total {total_pct:.2f}% alcanzó el límite de "
                f"{max_total_loss_percent}%: bot detenido."
            ),
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="total_drawdown",
        reason=(f"Drawdown total {total_pct:.2f}% dentro del límite de {max_total_loss_percent}%."),
    )


# ---------------------------------------------------------------------------
# Checks ADJUST_DOWN
# ---------------------------------------------------------------------------


def check_margin_cap(margin_usdt: Decimal, max_margin: Decimal) -> CheckResult:
    """Margen no puede superar el tope configurado."""
    if margin_usdt > max_margin:
        return CheckResult(
            outcome=CheckOutcome.ADJUST_DOWN,
            rule="margin_cap",
            reason=(
                f"Margen {margin_usdt} USDT supera el tope configurado de "
                f"{max_margin} USDT: se ajusta a la baja."
            ),
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="margin_cap",
        reason=f"Margen {margin_usdt} USDT dentro del tope de {max_margin} USDT.",
    )


def check_leverage_cap(
    leverage: int,
    config: AppConfig,
    environment: Environment,
) -> CheckResult:
    """Leverage no puede superar el tope del entorno."""
    cap = leverage_cap_for_env(config, environment)
    if leverage > cap:
        return CheckResult(
            outcome=CheckOutcome.ADJUST_DOWN,
            rule="leverage_cap",
            reason=(
                f"Leverage {leverage}x supera el tope de {cap}x para {environment}: "
                "se ajusta a la baja."
            ),
        )
    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="leverage_cap",
        reason=f"Leverage {leverage}x dentro del tope de {cap}x para {environment}.",
    )


def check_liquidation_safety(
    decision: ModelDecision,
    config: LiquidationSafetyConfig,
) -> CheckResult:
    """Verifica que el SL no quede más allá del precio de liquidación estimado.

    Precedencia de fallos:
    1. Check deshabilitado → PASS.
    2. NO_OPERAR → PASS (sin trade que validar).
    3. Distancia de liquidación desconocida (0%) → BLOCK si block_if_liquidation_unknown.
    4. SL más allá del precio de liquidación → BLOCK si block_if_stop_after_liquidation.
    5. Distancia entry→liquidación < mínimo → ADJUST_DOWN.
    6. Buffer SL→liquidación < mínimo → ADJUST_DOWN.
    """
    if not config.enabled:
        return CheckResult(
            outcome=CheckOutcome.PASS,
            rule="liquidation_safety",
            reason="Check de liquidación deshabilitado en configuración.",
        )

    if decision.decision == DecisionType.NO_OPERAR:
        return CheckResult(
            outcome=CheckOutcome.PASS,
            rule="liquidation_safety",
            reason="NO_OPERAR: no hay trade que validar.",
        )

    # check_sl_required bloquea esto upstream, pero como función pura independiente
    # puede recibir execute=True con sl=0 sin pasar por ese filtro.
    if decision.execute and decision.stop_loss <= 0:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="liquidation_safety",
            reason=(
                "SL ausente o cero: check_liquidation_safety requiere stop_loss > 0 "
                "cuando execute=True."
            ),
        )

    liq_dist = decision.liquidation_distance_percent_estimated

    if liq_dist == 0.0:
        if config.block_if_liquidation_unknown:
            return CheckResult(
                outcome=CheckOutcome.BLOCK,
                rule="liquidation_unknown",
                reason=(
                    "Distancia de liquidación desconocida (0%): block_if_liquidation_unknown=True."
                ),
            )
        return CheckResult(
            outcome=CheckOutcome.PASS,
            rule="liquidation_safety",
            reason="Distancia desconocida aceptada: block_if_liquidation_unknown=False.",
        )

    entry = decision.entry_price
    sl = decision.stop_loss
    direction = decision.decision

    if entry <= 0:
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="liquidation_safety",
            reason=(
                f"entry_price={entry} inválido: debe ser > 0 "
                "para calcular el precio de liquidación."
            ),
        )

    liq_price = _estimate_liquidation_price(entry, liq_dist, direction)

    # Rule 4: SL past liquidation (hard block)
    if _sl_is_past_liquidation(sl, liq_price, direction):
        if config.block_if_stop_after_liquidation:
            return CheckResult(
                outcome=CheckOutcome.BLOCK,
                rule="sl_after_liquidation",
                reason=(
                    f"{direction}: stop_loss={sl:.4f} está más allá del precio de "
                    f"liquidación estimado={liq_price:.4f}; trade bloqueado."
                ),
            )
        return CheckResult(
            outcome=CheckOutcome.PASS,
            rule="liquidation_safety",
            reason=(
                f"{direction}: SL más allá de liq aceptado: block_if_stop_after_liquidation=False."
            ),
        )

    # Rule 5: entry-to-liq distance
    if liq_dist < config.min_distance_entry_to_liquidation_percent:
        return CheckResult(
            outcome=CheckOutcome.ADJUST_DOWN,
            rule="entry_liq_distance",
            reason=(
                f"Distancia entry→liquidación ({liq_dist:.2f}%) menor al mínimo "
                f"requerido ({config.min_distance_entry_to_liquidation_percent:.2f}%): "
                "se requiere ajuste."
            ),
        )

    # Rule 6: SL-to-liq buffer
    buffer_pct = _sl_buffer_percent(sl, liq_price, direction)
    if buffer_pct < 0:
        # Asimetría de punto flotante: _sl_is_past_liquidation devolvió False
        # pero el SL está en o más allá de la liquidación.
        return CheckResult(
            outcome=CheckOutcome.BLOCK,
            rule="sl_after_liquidation",
            reason=(
                f"{direction}: buffer SL→liquidación negativo ({buffer_pct:.4f}%): "
                "el SL está en o más allá del precio de liquidación estimado."
            ),
        )
    if buffer_pct < config.min_buffer_stop_to_liquidation_percent:
        return CheckResult(
            outcome=CheckOutcome.ADJUST_DOWN,
            rule="sl_liq_buffer",
            reason=(
                f"{direction}: buffer SL→liquidación ({buffer_pct:.2f}%) menor al "
                f"mínimo ({config.min_buffer_stop_to_liquidation_percent:.2f}%): "
                "se requiere ajuste."
            ),
        )

    return CheckResult(
        outcome=CheckOutcome.PASS,
        rule="liquidation_safety",
        reason=(
            f"{direction}: buffer SL→liquidación={buffer_pct:.2f}%, "
            f"distancia entry→liq={liq_dist:.2f}%, "
            f"precio liquidación estimado={liq_price:.4f}."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _estimate_liquidation_price(
    entry_price: float,
    liq_dist_pct: float,
    direction: DecisionType,
) -> float:
    """Precio de liquidación estimado a partir de la distancia declarada.

    LONG:  liq = entry * (1 - dist/100)  — liquidación por debajo del entry
    SHORT: liq = entry * (1 + dist/100)  — liquidación por encima del entry
    """
    if direction == DecisionType.LONG:
        return entry_price * (1 - liq_dist_pct / 100)
    return entry_price * (1 + liq_dist_pct / 100)


def _sl_is_past_liquidation(sl: float, liq_price: float, direction: DecisionType) -> bool:
    """True si el SL está en el lado «malo» del precio de liquidación.

    LONG:  malo = sl <= liq_price  (SL en o bajo la liq → se liquida antes de activarse)
    SHORT: malo = sl >= liq_price  (SL en o sobre la liq → se liquida antes de activarse)
    """
    if direction == DecisionType.LONG:
        return sl <= liq_price
    return sl >= liq_price


def _sl_buffer_percent(sl: float, liq_price: float, direction: DecisionType) -> float:
    """Distancia porcentual entre el SL y el precio de liquidación.

    Siempre positivo cuando el SL está del lado correcto (not past liquidation).
    Expresado como % del precio de liquidación.
    """
    if direction == DecisionType.LONG:
        return (sl - liq_price) / liq_price * 100
    return (liq_price - sl) / liq_price * 100


# ---------------------------------------------------------------------------
# Checks anti-martingala / anti-averaging (F9 [74])
# Estas funciones usan interfaz dict | None para integración futura con el engine.
# ---------------------------------------------------------------------------


def check_anti_martingala(
    proposed_margin_usdt: Decimal,
    last_trade_pnl_usdt: Decimal | None,
    last_trade_margin_usdt: Decimal | None,
) -> dict[str, str] | None:
    """Bloquea el patrón martingala: aumentar el margen después de una pérdida.

    Detecta el patrón de doblar (o aumentar) la apuesta tras un trade perdedor,
    que viola la regla no negociable del proyecto (sección 2: "no martingala").

    Args:
        proposed_margin_usdt: Margen propuesto para el nuevo trade. Debe ser
            un Decimal positivo.
        last_trade_pnl_usdt: PnL realizado del último trade cerrado. None indica
            que no hay historial de trades — en ese caso no se bloquea.
        last_trade_margin_usdt: Margen utilizado en el último trade cerrado. None
            indica que no hay historial — en ese caso no se bloquea. Debe ser
            positivo si se provee.

    Returns:
        None si el trade es aceptable, o un dict de reasons si debe bloquearse.

    Raises:
        ValueError: Si proposed_margin_usdt no es un Decimal positivo, o si
            last_trade_margin_usdt no es un Decimal positivo cuando se provee.
    """
    if not isinstance(proposed_margin_usdt, Decimal):
        raise ValueError(
            "proposed_margin_usdt debe ser Decimal, "
            f"recibido: {type(proposed_margin_usdt).__name__}."
        )
    if proposed_margin_usdt <= Decimal("0"):
        raise ValueError(
            f"proposed_margin_usdt debe ser positivo, recibido: {proposed_margin_usdt}."
        )

    if last_trade_pnl_usdt is not None and not isinstance(last_trade_pnl_usdt, Decimal):
        raise ValueError(
            "last_trade_pnl_usdt debe ser Decimal o None, "
            f"recibido: {type(last_trade_pnl_usdt).__name__}."
        )
    if last_trade_margin_usdt is not None:
        if not isinstance(last_trade_margin_usdt, Decimal):
            raise ValueError(
                "last_trade_margin_usdt debe ser Decimal o None, "
                f"recibido: {type(last_trade_margin_usdt).__name__}."
            )
        if last_trade_margin_usdt <= Decimal("0"):
            raise ValueError(
                f"last_trade_margin_usdt debe ser positivo, "
                f"recibido: {last_trade_margin_usdt}."
            )

    # Sin historial suficiente: no se puede detectar el patrón → no bloquear
    if last_trade_pnl_usdt is None or last_trade_margin_usdt is None:
        return None

    # Solo aplica cuando el último trade fue perdedor
    if last_trade_pnl_usdt >= Decimal("0"):
        return None

    # Patrón martingala: aumentar el margen después de una pérdida
    if proposed_margin_usdt > last_trade_margin_usdt:
        return {
            "anti_martingala": (
                f"Martingala detectada: el último trade cerró con pérdida de "
                f"{last_trade_pnl_usdt} USDT (margen {last_trade_margin_usdt} USDT) "
                f"y el margen propuesto ({proposed_margin_usdt} USDT) es mayor. "
                "Aumentar el tamaño tras una pérdida está prohibido."
            )
        }

    return None


def check_anti_averaging(
    open_position_unrealized_pnl_usdt: Decimal | None,
) -> dict[str, str] | None:
    """Bloquea el promediado de pérdidas: operar sobre una posición abierta en rojo.

    Evita agregar exposición a una posición que ya está en pérdida no realizada,
    lo que viola la regla no negociable del proyecto (sección 2: "no promediar
    pérdidas").

    Args:
        open_position_unrealized_pnl_usdt: PnL no realizado de la posición abierta
            en el símbolo. None indica que no hay posición abierta — no se bloquea.
            Un valor negativo indica posición en pérdida.

    Returns:
        None si el trade es aceptable, o un dict de reasons si debe bloquearse.

    Raises:
        ValueError: Si open_position_unrealized_pnl_usdt no es Decimal ni None.
    """
    if open_position_unrealized_pnl_usdt is not None and not isinstance(
        open_position_unrealized_pnl_usdt, Decimal
    ):
        raise ValueError(
            f"open_position_unrealized_pnl_usdt debe ser Decimal o None, "
            f"recibido: {type(open_position_unrealized_pnl_usdt).__name__}."
        )

    # Sin posición abierta: nada que promediar
    if open_position_unrealized_pnl_usdt is None:
        return None

    # Posición en ganancia o breakeven: no bloquear
    if open_position_unrealized_pnl_usdt >= Decimal("0"):
        return None

    return {
        "anti_averaging": (
            f"Promediado de pérdidas detectado: la posición abierta tiene PnL "
            f"no realizado de {open_position_unrealized_pnl_usdt} USDT. "
            "Agregar exposición a una posición perdedora está prohibido."
        )
    }
