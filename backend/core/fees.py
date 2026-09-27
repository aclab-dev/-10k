"""Estimación de fees pre-trade para futuros perpetuos.

Módulo neutral de dominio, par de `backend/core/slippage.py`: lo usan el Risk
Engine, el ciclo real y el motor de replay. No depende de ningún adapter
concreto ni toca la DB.

Distinto de `backend/backtesting/fee_model.py`: ese modelo *cobra* el fee de
un fill ya simulado en PAPER. Este módulo *estima*, antes de operar, cuánto
costará el round-trip completo (entrada + salida) con las tasas reales que
reporta el adapter del exchange, y cuánto erosiona eso el edge del trade
(regla no negociable "sin cálculo de fees → no se opera", fila 12 de
`docs/live_checklist.md`).

Modelo
------
- **Entrada**: MARKET paga taker; LIMIT paga maker (agrega liquidez).
- **Salida**: siempre taker. El SL es STOP_MARKET y el TP es
  TAKE_PROFIT_MARKET: ambos se disparan a mercado. Es además el peor caso.
- La cantidad es `notional / entry_price`, la misma base que usa
  `ExecutionEngine._compute_quantity`. El fee de salida se calcula sobre el
  notional *al precio de salida* (SL o TP), no sobre el de entrada.

Con eso se obtiene el RR neto de fees:

    net_rr = (ganancia_bruta_al_TP − fee_entrada − fee_salida_TP)
             / (pérdida_bruta_al_SL + fee_entrada + fee_salida_SL)

Margen y leverage escalan numerador y denominador por igual, así que el RR
neto sólo depende de las distancias SL/TP y de las tasas. Por eso el gate que
lo consume bloquea en vez de ajustar: reducir margen o leverage no lo mejora.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_UP, Decimal

from backend.core.constants import QUANT
from backend.decision_engine.schemas import DecisionType, EntryType, ModelDecision

#: Cuantización del RR: 4 decimales alcanzan para comparar contra
#: `min_net_risk_reward` y leerlo en la auditoría. Siempre hacia abajo
#: (ROUND_FLOOR): redondear al par más cercano dejaría pasar un 1.49995 como
#: 1.5000, un fail-open justo en el umbral.
_RR_QUANT = Decimal("0.0001")

#: Identificador del método, persistido en la auditoría para que un replay sepa
#: con qué modelo se calculó el número (si la fórmula cambia, cambia el id).
ESTIMATION_METHOD = "entry_by_type_exit_taker_v1"


class FeeRatesUnavailableError(Exception):
    """El adapter no pudo obtener tasas de fee válidas del exchange.

    Error agnóstico del exchange: cada adapter envuelve en este sus fallas
    propias (API, red, payload malformado) para que el ciclo pueda tratarlas
    como "sin estimación de fees" y bloquear el trade con auditoría, sin
    acoplarse a los errores de un adapter concreto.
    """


@dataclass(frozen=True)
class FeeRates:
    """Tasas de fee del exchange, como fracción del notional (0.0005 = 0.05%).

    Las provee `ExchangeAdapter.get_fee_rates`: nunca se hardcodean en el
    Risk Engine.
    """

    maker: Decimal
    taker: Decimal

    def __post_init__(self) -> None:
        # Una tasa negativa (rebate) o >= 100% del notional es un dato corrupto
        # del exchange, no un fee: mejor fallar que estimar sobre basura.
        for name, rate in (("maker", self.maker), ("taker", self.taker)):
            if not Decimal("0") <= rate < Decimal("1"):
                raise ValueError(f"fee rate {name} fuera de [0, 1): {rate}")


@dataclass(frozen=True)
class FeeEstimate:
    """Fees proyectados del round-trip y RR neto resultante.

    Importes en USDT, magnitudes absolutas (coste esperado). Los de salida se
    desglosan por escenario porque el notional de salida depende de si cierra
    el SL o el TP.
    """

    entry_fee_usdt: Decimal
    exit_fee_at_tp_usdt: Decimal
    exit_fee_at_sl_usdt: Decimal
    gross_risk_reward: Decimal
    net_risk_reward: Decimal
    entry_rate: Decimal
    exit_rate: Decimal
    method: str = ESTIMATION_METHOD

    @property
    def round_trip_fee_at_sl_usdt(self) -> Decimal:
        """Fee total si el trade cierra en el SL: el que agrava la pérdida."""
        return self.entry_fee_usdt + self.exit_fee_at_sl_usdt

    def as_audit_reason(self) -> str:
        """Línea legible para `RiskValidationResult.reasons` (Anexo B)."""
        return (
            f"Fees proyectados: entrada {self.entry_fee_usdt} USDT (tasa {self.entry_rate}), "
            f"salida {self.exit_fee_at_tp_usdt} USDT al TP / {self.exit_fee_at_sl_usdt} USDT "
            f"al SL (tasa {self.exit_rate}). RR bruto {self.gross_risk_reward}, "
            f"RR neto de fees {self.net_risk_reward}. Método: {self.method}."
        )


def estimate_fees(
    *,
    side: DecisionType,
    entry_type: EntryType,
    notional_usdt: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    rates: FeeRates,
) -> FeeEstimate:
    """Estima los fees del round-trip y el RR neto de un trade propuesto.

    Args:
        side: LONG o SHORT.
        entry_type: MARKET paga taker en la entrada; LIMIT paga maker.
        notional_usdt: margen × leverage. Debe ser > 0.
        entry_price: precio de entrada. Debe ser > 0.
        stop_loss: precio del SL, del lado de la pérdida respecto de la entrada.
        take_profit: precio del TP, del lado de la ganancia.
        rates: tasas reales del adapter.

    Raises:
        ValueError: si algún input está fuera de rango o SL/TP no son
            coherentes con el lado. No se devuelve una estimación degradada:
            un número inventado en la auditoría es peor que un error explícito.
    """
    if side not in (DecisionType.LONG, DecisionType.SHORT):
        raise ValueError(f"side debe ser LONG o SHORT, recibido {side}")
    if entry_type not in (EntryType.MARKET, EntryType.LIMIT):
        raise ValueError(f"entry_type debe ser MARKET o LIMIT, recibido {entry_type}")
    if notional_usdt <= 0:
        raise ValueError(f"notional_usdt debe ser > 0, recibido {notional_usdt}")
    if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
        raise ValueError(
            f"precios deben ser > 0: entry={entry_price}, sl={stop_loss}, tp={take_profit}"
        )

    if side == DecisionType.LONG:
        reward_per_unit = take_profit - entry_price
        risk_per_unit = entry_price - stop_loss
    else:
        reward_per_unit = entry_price - take_profit
        risk_per_unit = stop_loss - entry_price
    if reward_per_unit <= 0 or risk_per_unit <= 0:
        raise ValueError(
            f"SL/TP incoherentes para {side}: entry={entry_price}, sl={stop_loss}, tp={take_profit}"
        )

    entry_rate = rates.taker if entry_type == EntryType.MARKET else rates.maker
    exit_rate = rates.taker
    quantity = notional_usdt / entry_price

    entry_fee = notional_usdt * entry_rate
    exit_fee_tp = quantity * take_profit * exit_rate
    exit_fee_sl = quantity * stop_loss * exit_rate

    # Los RR se calculan por unidad de contrato (dividiendo todo por la
    # cantidad): son cocientes exactos de precios y tasas. Pasar por
    # `quantity = notional / entry_price`, que no suele ser exacto en Decimal,
    # haría que un RR de 2 exacto saliera 1.99999… y el floor lo dejara en 1.9999.
    net_reward_per_unit = reward_per_unit - entry_price * entry_rate - take_profit * exit_rate
    net_risk_per_unit = risk_per_unit + entry_price * entry_rate + stop_loss * exit_rate

    # Fees redondeados hacia arriba: ante la duda, el costo proyectado es mayor.
    return FeeEstimate(
        entry_fee_usdt=entry_fee.quantize(QUANT, rounding=ROUND_UP),
        exit_fee_at_tp_usdt=exit_fee_tp.quantize(QUANT, rounding=ROUND_UP),
        exit_fee_at_sl_usdt=exit_fee_sl.quantize(QUANT, rounding=ROUND_UP),
        gross_risk_reward=(reward_per_unit / risk_per_unit).quantize(
            _RR_QUANT, rounding=ROUND_FLOOR
        ),
        # net_risk > 0 siempre (risk > 0 y fees >= 0). net_reward puede
        # ser negativo si los fees se comen el TP entero: el RR queda negativo
        # y el gate lo bloquea igual.
        net_risk_reward=(net_reward_per_unit / net_risk_per_unit).quantize(
            _RR_QUANT, rounding=ROUND_FLOOR
        ),
        entry_rate=entry_rate,
        exit_rate=exit_rate,
    )


def estimate_fees_for_decision(
    decision: ModelDecision,
    margin_usdt: Decimal,
    leverage: int,
    rates: FeeRates,
) -> FeeEstimate:
    """Adapta un ModelDecision a `estimate_fees`.

    Precondición: la decisión describe una orden (`is_estimable` de
    `backend.core.slippage`). Los callers la filtran igual que con el slippage.
    """
    return estimate_fees(
        side=decision.decision,
        entry_type=decision.entry_type,
        notional_usdt=margin_usdt * leverage,
        entry_price=Decimal(str(decision.entry_price)),
        stop_loss=Decimal(str(decision.stop_loss)),
        take_profit=Decimal(str(decision.take_profit)),
        rates=rates,
    )


__all__ = [
    "ESTIMATION_METHOD",
    "FeeEstimate",
    "FeeRates",
    "FeeRatesUnavailableError",
    "estimate_fees",
    "estimate_fees_for_decision",
]
