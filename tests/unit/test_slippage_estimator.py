"""Unit tests — estimación de slippage pre-trade (F17 [162], regla no negociable 13)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.slippage import ESTIMATION_METHOD, SlippageEstimate, estimate_slippage
from backend.exchange_adapters.schemas import OrderSide, OrderType

_D = Decimal
_QUANT = _D("0.00000001")

# Escenario base: mid = 100, spread = 0.20 (media horquilla 0.10), impacto 2 BPS.
# Con notional 100 USDT y precio de referencia 100, la cantidad es 1 unidad, así
# que el coste por unidad y el coste total coinciden numéricamente — eso mantiene
# los asserts legibles sin esconder la multiplicación por cantidad.
_BID = _D("99.90")
_ASK = _D("100.10")
_REF = _D("100")
_IMPACT_BPS = _D("2")


def _estimate(
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    notional: Decimal = _D("100"),
    bid: Decimal = _BID,
    ask: Decimal = _ASK,
    reference_price: Decimal = _REF,
    impact_bps: Decimal = _IMPACT_BPS,
) -> SlippageEstimate:
    return estimate_slippage(
        side=side,
        order_type=order_type,
        notional_usdt=notional,
        bid=bid,
        ask=ask,
        reference_price=reference_price,
        market_impact_bps=impact_bps,
    )


class TestComponentes:
    def test_media_horquilla_es_medio_spread_por_cantidad(self) -> None:
        # (100.10 - 99.90) / 2 = 0.10 por unidad × 1 unidad
        assert _estimate().half_spread_usdt == _D("0.10000000")

    def test_impacto_es_los_bps_sobre_el_precio_de_referencia(self) -> None:
        # 2 BPS de 100 = 0.02 por unidad × 1 unidad
        assert _estimate().impact_usdt == _D("0.02000000")

    def test_total_es_la_suma_de_los_componentes(self) -> None:
        est = _estimate()
        assert est.estimated_slippage_usdt == _D("0.12000000")
        assert est.estimated_slippage_usdt == est.half_spread_usdt + est.impact_usdt

    def test_impacto_cero_deja_solo_la_media_horquilla(self) -> None:
        est = _estimate(impact_bps=_D("0"))
        assert est.impact_usdt == _D("0")
        assert est.estimated_slippage_usdt == est.half_spread_usdt

    def test_expone_el_metodo_para_auditoria(self) -> None:
        assert _estimate().method == ESTIMATION_METHOD


class TestDireccionAdversa:
    def test_buy_espera_llenar_por_encima_de_la_referencia(self) -> None:
        assert _estimate(side=OrderSide.BUY).expected_fill_price == _D("100.12000000")

    def test_sell_espera_llenar_por_debajo_de_la_referencia(self) -> None:
        assert _estimate(side=OrderSide.SELL).expected_fill_price == _D("99.88000000")

    def test_el_coste_es_el_mismo_en_ambos_lados(self) -> None:
        # El slippage es una magnitud absoluta (coste), no un valor signado:
        # misma convención que OrderResult.slippage_usdt, que es ge=0.
        buy = _estimate(side=OrderSide.BUY).estimated_slippage_usdt
        sell = _estimate(side=OrderSide.SELL).estimated_slippage_usdt
        assert buy == sell > _D("0")


class TestTipoDeOrden:
    def test_limit_no_estima_slippage(self) -> None:
        est = _estimate(order_type=OrderType.LIMIT)
        assert est.estimated_slippage_usdt == _D("0")
        assert est.half_spread_usdt == _D("0")
        assert est.impact_usdt == _D("0")

    def test_limit_espera_llenar_al_precio_de_referencia(self) -> None:
        assert _estimate(order_type=OrderType.LIMIT).expected_fill_price == _REF

    @pytest.mark.parametrize("order_type", [OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET])
    def test_stop_y_tp_no_estiman_contra_este_libro(self, order_type: OrderType) -> None:
        # Se disparan a mercado recién cuando toca el trigger, momento en el que
        # el bid/ask de este snapshot ya no describe el libro.
        assert _estimate(order_type=order_type).estimated_slippage_usdt == _D("0")


class TestEscala:
    def test_escala_linealmente_con_el_notional(self) -> None:
        chico = _estimate(notional=_D("100")).estimated_slippage_usdt
        grande = _estimate(notional=_D("1000")).estimated_slippage_usdt
        assert grande == chico * 10

    def test_un_spread_mas_ancho_cuesta_mas(self) -> None:
        angosto = _estimate(bid=_D("99.99"), ask=_D("100.01")).estimated_slippage_usdt
        ancho = _estimate(bid=_D("99.50"), ask=_D("100.50")).estimated_slippage_usdt
        assert ancho > angosto

    def test_resultado_cuantizado_a_8_decimales(self) -> None:
        est = _estimate(notional=_D("33.33333333"), reference_price=_D("31337.77"))
        assert est.estimated_slippage_usdt == est.estimated_slippage_usdt.quantize(_QUANT)
        assert est.expected_fill_price == est.expected_fill_price.quantize(_QUANT)


class TestInputsInvalidos:
    """Un número inventado en la auditoría es peor que un error explícito."""

    def test_notional_cero_es_error(self) -> None:
        with pytest.raises(ValueError, match="notional_usdt"):
            _estimate(notional=_D("0"))

    def test_notional_negativo_es_error(self) -> None:
        with pytest.raises(ValueError, match="notional_usdt"):
            _estimate(notional=_D("-10"))

    def test_bid_mayor_o_igual_que_ask_es_error(self) -> None:
        with pytest.raises(ValueError, match="debe ser menor que ask"):
            _estimate(bid=_D("100.10"), ask=_D("100.10"))

    def test_bid_no_positivo_es_error(self) -> None:
        with pytest.raises(ValueError, match="bid y ask"):
            _estimate(bid=_D("0"), ask=_D("100.10"))

    def test_reference_price_no_positivo_es_error(self) -> None:
        with pytest.raises(ValueError, match="reference_price"):
            _estimate(reference_price=_D("0"))

    def test_impact_bps_negativo_es_error(self) -> None:
        with pytest.raises(ValueError, match="market_impact_bps"):
            _estimate(impact_bps=_D("-1"))


class TestReasonDeAuditoria:
    def test_incluye_el_valor_los_componentes_y_el_metodo(self) -> None:
        reason = _estimate().as_audit_reason()
        assert "0.12000000" in reason
        assert "0.10000000" in reason
        assert "0.02000000" in reason
        assert ESTIMATION_METHOD in reason
