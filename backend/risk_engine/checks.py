"""Validaciones determinísticas del Risk Engine por entorno operativo.

Cada función retorna un dict reasons (rule_name → explicación) si la validación
falla, o None si el parámetro es aceptable. El armado del RiskValidationResult
completo es responsabilidad del engine (engine.py).
"""

from typing import assert_never

from backend.core.config import Environment

# Caps de leverage por entorno (reglas no negociables — sección 2 del proyecto)
_LEVERAGE_CAPS: dict[str, int] = {
    "PAPER": 10,
    "TESTNET": 5,
    "LIVE_INITIAL": 3,
    "LIVE_ABSOLUTE": 5,
}


def check_leverage_cap(
    leverage: int,
    environment: Environment | str,
    # Default conservador: siempre el cap más bajo (LIVE inicial = 3x).
    # Cambiar a False solo cuando el bot haya superado la fase inicial.
    is_live_initial: bool = True,
) -> dict[str, str] | None:
    """Valida que el leverage propuesto respete el cap del entorno activo.

    Args:
        leverage: Leverage propuesto. Debe ser un entero >= 1.
        environment: Entorno operativo. Debe ser un valor de Environment.
        is_live_initial: En LIVE, True aplica cap inicial (3x, el más
            restrictivo); False aplica cap absoluto (5x). Default True.

    Returns:
        None si el leverage es aceptable, o un dict de reasons si debe bloquearse.

    Raises:
        ValueError: Si leverage <= 0, o si el entorno no es reconocido.
    """
    if not isinstance(environment, Environment):
        if environment is None:
            raise ValueError("environment no puede ser None.")
        try:
            environment = Environment(str(environment).upper())
        except ValueError as exc:
            raise ValueError(
                f"Entorno no reconocido: '{environment}'. "
                f"Valores válidos: {[e.value for e in Environment]}"
            ) from exc

    if leverage <= 0:
        raise ValueError(f"leverage debe ser un entero positivo, recibido: {leverage}.")

    if environment == Environment.PAPER:
        cap = _LEVERAGE_CAPS["PAPER"]
        rule = "leverage_cap_paper"
    elif environment == Environment.TESTNET:
        cap = _LEVERAGE_CAPS["TESTNET"]
        rule = "leverage_cap_testnet"
    elif environment == Environment.LIVE:
        if is_live_initial:
            cap = _LEVERAGE_CAPS["LIVE_INITIAL"]
            rule = "leverage_cap_live_initial"
        else:
            cap = _LEVERAGE_CAPS["LIVE_ABSOLUTE"]
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
