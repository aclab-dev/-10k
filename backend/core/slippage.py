"""Estimación de slippage pre-trade para futuros perpetuos.

Módulo neutral de dominio: puede ser usado por el Risk Engine, el Execution
Engine, el motor de replay y el de backtesting. No depende de ningún adapter
concreto ni toca la DB.

Distinto de `backend/backtesting/slippage_model.py`: ese modelo *simula* un
fill en PAPER aplicando BPS fijos a un precio de referencia. Este módulo
*estima*, antes de operar, cuánto costaría cruzar el spread con el notional
propuesto, para que el Risk Engine tenga el número y quede en auditoría
(regla no negociable "sin estimación de fees, slippage y funding → no se
opera", fila 13 de `docs/live_checklist.md`).

Heurística (documentada, no order book depth)
---------------------------------------------
El codebase no tiene profundidad de libro: `MarketSnapshot` expone `bid`,
`ask` y `spread_*`, y ningún fetcher trae los niveles del order book. La
estimación se apoya entonces en los dos únicos datos disponibles:

1. **Media horquilla** — una orden MARKET se ejecuta contra el otro lado del
   libro, así que paga `(ask - bid) / 2` por unidad respecto del mid. Es el
   piso del coste: existe aunque el tamaño sea despreciable.
2. **Impacto de mercado** — colchón configurable en basis points sobre el
   precio de referencia (`slippage.market_impact_bps`), que representa cuánto
   se corre el precio al consumir niveles. Sin profundidad de libro no se
   puede modelar como función del tamaño; se toma constante y se documenta
   como tal.

`slippage_usdt = notional_usdt × (half_spread / reference_price + impact_bps / 10 000)`

Trabajar sobre el notional en vez de la cantidad evita duplicar la fórmula de
`ExecutionEngine._compute_quantity` en cada call site. La diferencia contra el
valor real es el redondeo ROUND_DOWN de la cantidad ejecutada (< 1e-8 × precio),
despreciable frente al propio error de la heurística.

Las órdenes LIMIT estiman 0: o llenan al precio indicado o no llenan.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.decision_engine.schemas import DecisionType, EntryType, ModelDecision
from backend.exchange_adapters.schemas import OrderSide, OrderType
from backend.market_data.schemas import MarketSnapshot

_BASIS_POINTS = Decimal("10000")
_QUANT = Decimal("0.00000001")
_TWO = Decimal("2")

#: Tipos de orden que cruzan el spread. El resto llena al precio indicado (LIMIT)
#: o se dispara a mercado recién cuando toca el trigger (STOP_*), momento en el
#: que el bid/ask de este snapshot ya no describe el libro.
_CROSSES_SPREAD: frozenset[OrderType] = frozenset({OrderType.MARKET})

#: Identificador del método, persistido en la auditoría para que un replay sepa
#: con qué heurística se calculó el número (si la fórmula cambia, cambia el id).
ESTIMATION_METHOD = "half_spread_plus_fixed_impact_bps_v1"


@dataclass(frozen=True)
class SlippageEstimate:
    """Estimación de slippage pre-trade, con sus componentes desglosados.

    Todos los importes son magnitudes absolutas en USDT (coste esperado),
    nunca signadas: la misma convención que `OrderResult.slippage_usdt`.
    """

    estimated_slippage_usdt: Decimal
    half_spread_usdt: Decimal
    impact_usdt: Decimal
    expected_fill_price: Decimal
    method: str = ESTIMATION_METHOD

    def as_audit_reason(self) -> str:
        """Línea legible para `RiskValidationResult.reasons` (Anexo B).

        El Risk Engine audita en `reasons: dict[str, str]`, así que el número
        viaja formateado. El valor comparable máquina-a-máquina vive en
        `orders.estimated_slippage_usdt`.
        """
        return (
            f"Slippage estimado pre-trade: {self.estimated_slippage_usdt} USDT "
            f"(media horquilla {self.half_spread_usdt} + impacto {self.impact_usdt}), "
            f"fill esperado {self.expected_fill_price}. Método: {self.method}."
        )


def estimate_slippage(
    side: OrderSide,
    order_type: OrderType,
    notional_usdt: Decimal,
    bid: Decimal,
    ask: Decimal,
    reference_price: Decimal,
    market_impact_bps: Decimal,
) -> SlippageEstimate:
    """Estima el coste de slippage de cruzar el spread con *notional_usdt*.

    Args:
        side: BUY paga el ask (fill esperado por encima), SELL recibe el bid.
        order_type: sólo MARKET cruza el spread; el resto estima 0.
        notional_usdt: margen × leverage, en USDT. Debe ser > 0.
        bid: mejor bid del snapshot. Debe ser > 0 y < *ask*.
        ask: mejor ask del snapshot.
        reference_price: precio contra el que se expresa el coste relativo
            (el `last_price` del snapshot o el `entry_price` de la decisión).
            Debe ser > 0.
        market_impact_bps: colchón de impacto en basis points. Debe ser >= 0.

    Returns:
        SlippageEstimate con el total y sus componentes, cuantizados a 8
        decimales.

    Raises:
        ValueError: si algún input está fuera de rango o los precios son
            incoherentes (`bid >= ask`). No se devuelve una estimación
            degradada: un número inventado en la auditoría es peor que un
            error explícito.
    """
    if notional_usdt <= 0:
        raise ValueError(f"notional_usdt debe ser > 0, recibido {notional_usdt}")
    if bid <= 0 or ask <= 0:
        raise ValueError(f"bid y ask deben ser > 0, recibidos bid={bid}, ask={ask}")
    if bid >= ask:
        raise ValueError(f"bid={bid} debe ser menor que ask={ask}")
    if reference_price <= 0:
        raise ValueError(f"reference_price debe ser > 0, recibido {reference_price}")
    if market_impact_bps < 0:
        raise ValueError(f"market_impact_bps debe ser >= 0, recibido {market_impact_bps}")

    if order_type not in _CROSSES_SPREAD:
        return SlippageEstimate(
            estimated_slippage_usdt=Decimal("0"),
            half_spread_usdt=Decimal("0"),
            impact_usdt=Decimal("0"),
            expected_fill_price=reference_price.quantize(_QUANT),
        )

    quantity = notional_usdt / reference_price
    half_spread_per_unit = (ask - bid) / _TWO
    impact_per_unit = reference_price * market_impact_bps / _BASIS_POINTS
    adverse_move_per_unit = half_spread_per_unit + impact_per_unit

    half_spread_usdt = (half_spread_per_unit * quantity).quantize(_QUANT)
    impact_usdt = (impact_per_unit * quantity).quantize(_QUANT)
    # Cuantizar el total aparte (y no sumando los componentes ya cuantizados)
    # mantiene el total fiel al cálculo exacto; la diferencia con la suma de
    # los componentes es como mucho 1 ulp.
    total_usdt = (adverse_move_per_unit * quantity).quantize(_QUANT)

    # BUY paga por encima del precio de referencia, SELL recibe por debajo.
    if side == OrderSide.BUY:
        expected_fill_price = reference_price + adverse_move_per_unit
    else:
        expected_fill_price = reference_price - adverse_move_per_unit

    return SlippageEstimate(
        estimated_slippage_usdt=total_usdt,
        half_spread_usdt=half_spread_usdt,
        impact_usdt=impact_usdt,
        expected_fill_price=expected_fill_price.quantize(_QUANT),
    )


def estimate_for_decision(
    snapshot: MarketSnapshot,
    decision: ModelDecision,
    margin_usdt: Decimal,
    leverage: int,
    market_impact_bps: Decimal,
) -> SlippageEstimate:
    """Adapta un ModelDecision + MarketSnapshot a `estimate_slippage`.

    Existe para que los dos call sites reales (`CycleRunner._process_symbol` y
    `HistoricalReplayEngine.run`) no dupliquen el mapeo decisión→orden ni el
    cálculo del notional, y estimen exactamente sobre los mismos parámetros que
    el Execution Engine va a ejecutar.

    Precondición: la decisión tiene que ser ejecutable (`execute=True`). Una
    NO_OPERAR admite `margin_usdt=0` y `entry_price=0` por schema, y no hay
    trade cuyo slippage estimar; el caller la filtra antes (el Risk Engine
    tampoco llegaría a usar el dato: su Fase 0 retorna antes de los checks).

    Args:
        snapshot: snapshot del que salen bid/ask. Su `symbol` debe coincidir
            con el de la decisión.
        decision: decisión a estimar. LONG mapea a BUY, SHORT a SELL.
        margin_usdt: margen aprobado por el Risk Engine (el ajustado, no el
            original, cuando hubo ADJUST_DOWN).
        leverage: leverage aprobado por el Risk Engine.
        market_impact_bps: `config.slippage.market_impact_bps`.

    Returns:
        SlippageEstimate para el notional `margin_usdt × leverage`.

    Raises:
        ValueError: si la decisión no es ejecutable, si el snapshot es de otro
            símbolo, o si algún parámetro está fuera de rango (ver
            `estimate_slippage`).
    """
    if not decision.execute:
        raise ValueError(
            f"No hay slippage que estimar para una decisión no ejecutable "
            f"(decision_id={decision.decision_id}, execute=False): sus "
            "margin_usdt y entry_price pueden ser 0 por schema. El caller debe "
            "filtrarla antes de estimar."
        )
    if snapshot.symbol != decision.symbol:
        raise ValueError(
            f"El snapshot es de {snapshot.symbol} pero la decisión es de "
            f"{decision.symbol}: estimar slippage con el libro de otro par "
            "daría un número sin sentido."
        )
    side = OrderSide.BUY if decision.decision == DecisionType.LONG else OrderSide.SELL
    order_type = OrderType.MARKET if decision.entry_type == EntryType.MARKET else OrderType.LIMIT
    return estimate_slippage(
        side=side,
        order_type=order_type,
        notional_usdt=margin_usdt * leverage,
        bid=snapshot.bid,
        ask=snapshot.ask,
        # entry_price, no last_price: es el precio contra el que el Execution
        # Engine calcula la cantidad (`_compute_quantity`), así que el notional
        # y el precio de referencia quedan expresados sobre la misma base.
        reference_price=Decimal(str(decision.entry_price)),
        market_impact_bps=market_impact_bps,
    )


__all__ = [
    "ESTIMATION_METHOD",
    "SlippageEstimate",
    "estimate_for_decision",
    "estimate_slippage",
]
