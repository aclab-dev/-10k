"""Validaciones determinísticas del Risk Engine por entorno operativo.

Cada función retorna un dict reasons (rule_name → explicación) si la validación
falla, o None si el parámetro es aceptable. El armado del RiskValidationResult
completo es responsabilidad del engine (engine.py).
"""

from decimal import Decimal
from typing import assert_never

from backend.core.config import Environment

# Caps de leverage por entorno (reglas no negociables — sección 2 del proyecto)
_LEVERAGE_CAP_PAPER = 10
_LEVERAGE_CAP_TESTNET = 5
_LEVERAGE_CAP_LIVE_INITIAL = 3
_LEVERAGE_CAP_LIVE_ABSOLUTE = 5


def check_leverage_cap(
    leverage: int,
    environment: Environment | str,
    is_live_initial: bool = True,
) -> dict[str, str] | None:
    """Valida que el leverage propuesto respete el cap del entorno activo.

    Args:
        leverage: Leverage propuesto. Debe ser un entero >= 1 (no bool, no float).
        environment: Entorno operativo. Debe ser un valor de Environment o su
            equivalente en string ("PAPER", "TESTNET", "LIVE"). Strings como
            "LIVE_INITIAL" no son entornos válidos y levantan ValueError.
        is_live_initial: En LIVE, True aplica el cap inicial (3x, el más
            restrictivo); False aplica el cap absoluto (5x). Default True —
            cambiar a False solo cuando el bot haya superado la fase inicial.

    Returns:
        None si el leverage es aceptable, o un dict de reasons si debe bloquearse.

    Raises:
        ValueError: Si leverage no es un int positivo, o si el entorno no es reconocido.
    """
    if not isinstance(environment, Environment):
        original_env = environment
        try:
            environment = Environment(str(environment).upper())
        except ValueError as exc:
            raise ValueError(
                f"Entorno no reconocido: {original_env!r}. "
                f"Valores válidos: {[e.value for e in Environment]}"
            ) from exc

    if isinstance(leverage, bool) or not isinstance(leverage, int):
        raise ValueError(f"leverage debe ser un entero, recibido: {type(leverage).__name__}.")
    if leverage <= 0:
        raise ValueError(f"leverage debe ser un entero positivo, recibido: {leverage}.")

    if environment != Environment.LIVE and not is_live_initial:
        raise ValueError(
            f"is_live_initial solo aplica en Environment.LIVE, recibido: {environment.value}."
        )

    if environment == Environment.PAPER:
        cap = _LEVERAGE_CAP_PAPER
        rule = "leverage_cap_paper"
    elif environment == Environment.TESTNET:
        cap = _LEVERAGE_CAP_TESTNET
        rule = "leverage_cap_testnet"
    elif environment == Environment.LIVE:
        if is_live_initial:
            cap = _LEVERAGE_CAP_LIVE_INITIAL
            rule = "leverage_cap_live_initial"
        else:
            cap = _LEVERAGE_CAP_LIVE_ABSOLUTE
            rule = "leverage_cap_live_absolute"
    else:
        assert_never(environment)

    if leverage > cap:
        suffix = " (fase inicial)" if environment == Environment.LIVE and is_live_initial else ""
        return {
            rule: (
                f"Leverage {leverage}x supera el máximo permitido de {cap}x "
                f"para entorno {environment.value}{suffix}."
            )
        }

    return None


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
                f"last_trade_margin_usdt debe ser positivo, recibido: {last_trade_margin_usdt}."
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
            f"Promediado de pérdidas detectado: la posición abierta tiene PnL no realizado "
            f"de {open_position_unrealized_pnl_usdt} USDT. "
            "Agregar exposición a una posición perdedora está prohibido."
        )
    }
