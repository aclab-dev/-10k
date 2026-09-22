"""Tests unitarios — slippage pre-trade en el Risk Engine (F17 [162], regla 13).

Cubre las dos mitades del wiring: el check informativo (`check_slippage_estimate`,
que nunca puede alterar la decisión) y el adaptador decisión→estimación
(`estimate_for_decision`, que garantiza que se estima sobre los mismos
parámetros que el Execution Engine va a ejecutar).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.config import AppConfig, Environment, get_config
from backend.core.slippage import (
    ESTIMATION_METHOD,
    SlippageEstimate,
    estimate_for_decision,
    half_spread,
    is_estimable,
)
from backend.decision_engine.aggregator_schemas import (
    ContributingSources,
    DecisionAggregationResult,
)
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    ModelDecision,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.schemas import PrimaryRegime
from backend.risk_engine.checks import CheckOutcome, check_slippage_estimate
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import RiskDecision, RiskValidationResult

_D = Decimal

# Entry price 95 000 con bid/ask 94 990 / 95 010: media horquilla 10, impacto
# 2 BPS = 19. Notional 25 USDT (5 × 5x) → cantidad 25/95 000 unidades.
_BID = _D("94990")
_ASK = _D("95010")
_ENTRY_PRICE = 95000.0


def _now() -> datetime:
    return datetime.now(UTC)


def _candle() -> CandleData:
    return CandleData(
        open=_D("95000"),
        high=_D("95100"),
        low=_D("94900"),
        close=_D("95000"),
        volume=_D("100"),
        n_candles=10,
    )


def _snapshot(**overrides: object) -> MarketSnapshot:
    defaults: dict[str, object] = {
        "timestamp_utc": _now(),
        "exchange": Exchange.BINGX,
        "environment": Environment.PAPER,
        "symbol": "BTCUSDT",
        "last_price": _D("95000"),
        "bid": _BID,
        "ask": _ASK,
        "spread_absolute": _ASK - _BID,
        "spread_percent": (_ASK - _BID) / _BID * 100,
        "candles": Candles(tf_5m=_candle(), tf_15m=_candle(), tf_1h=_candle(), tf_4h=_candle()),
        "volume": _D("1000"),
        "account_balance_usdt": _D("500"),
        "open_positions_count": 0,
        "active_orders_count": 0,
        "latency_ms": 50,
        "exchange_server_time": _now(),
        "local_time": _now(),
        "clock_skew_ms": 10,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshot(**defaults)  # type: ignore[arg-type]


def _quant_signals() -> QuantSignalsSection:
    return QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )


def _long_decision(**overrides: object) -> ModelDecision:
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "LONG",
        "symbol": "BTCUSDT",
        "entry_type": "MARKET",
        "entry_price": _ENTRY_PRICE,
        "stop_loss": 90000.0,
        "take_profit": 105000.0,
        "invalidation_price": 89000.0,
        "leverage": 5,
        "margin_usdt": 5.0,
        "estimated_notional_usdt": 25.0,
        "estimated_entry_fee_usdt": 0.025,
        "estimated_exit_fee_usdt": 0.025,
        "estimated_slippage_usdt": 0.05,
        "estimated_funding_usdt": -0.01,
        "net_risk_reward": 2.0,
        "estimated_max_loss_usdt": 5.0,
        "liquidation_distance_percent_estimated": 18.0,
        "confidence": 0.82,
        "market_regime": PrimaryRegime.TRENDING.value,
        "setup_name": "momentum_breakout",
        "timeframes_used": ["15m", "1h", "4h"],
        "quant_signals": _quant_signals().model_dump(),
        "decision_aggregator": DecisionAggregatorSection(
            quant_score=0.8,
            gpt_context_score=0.85,
            risk_quality_score=0.75,
            final_trade_quality_score=0.80,
        ).model_dump(),
        "news_context": NewsContextSection(
            used=False, impact=NewsImpact.NEUTRAL, summary="No news."
        ).model_dump(),
        "position_management_plan": PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=True,
            partial_close_plan="none",
            max_time_in_trade_minutes=120,
        ).model_dump(),
        "decision_rationale_summary": "Strong momentum with confirmed breakout.",
        "execute": True,
    }
    base.update(overrides)
    return ModelDecision.model_validate(base)


def _aggregation(decision: ModelDecision) -> DecisionAggregationResult:
    return DecisionAggregationResult(
        decision_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=_now(),
        contributing_sources=ContributingSources(
            quant_score=0.80,
            gpt_context_score=0.85,
            regime_factor=0.75,
            volatility_factor=0.70,
        ),
        aggregated_score=0.78,
        final_action=DecisionType.LONG,
    )


def _config() -> AppConfig:
    return get_config()


#: Funding que no dispara el gate de #133 (por debajo de max_adverse_funding_rate).
#: Estos tests son sobre el registro del slippage; el gate tiene los suyos.
_NEUTRAL_FUNDING_RATE = 0.0001


def _validate_neutral_funding(**kwargs: object) -> RiskValidationResult:
    """`engine.validate` con funding neutro, para aislar lo que estos tests miden."""
    kwargs.setdefault("funding_rate", _NEUTRAL_FUNDING_RATE)
    return validate(**kwargs)  # type: ignore[arg-type]


def _estimate_for(decision: ModelDecision, **overrides: object) -> SlippageEstimate:
    kwargs: dict[str, object] = {
        "snapshot": _snapshot(),
        "decision": decision,
        "margin_usdt": _D(str(decision.margin_usdt)),
        "leverage": decision.leverage,
        "market_impact_bps": _D("2"),
    }
    kwargs.update(overrides)
    return estimate_for_decision(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# check_slippage_estimate — informativo, nunca bloquea
# ---------------------------------------------------------------------------


class TestCheckSlippageEstimate:
    def test_pasa_con_estimacion(self) -> None:
        result = check_slippage_estimate(_estimate_for(_long_decision()))
        assert result.outcome == CheckOutcome.PASS
        assert result.rule == "slippage_estimate"

    def test_pasa_sin_estimacion(self) -> None:
        # La regla 13 pide estimar y registrar, no vetar: un None se asienta
        # en la auditoría pero no puede frenar el trade.
        result = check_slippage_estimate(None)
        assert result.outcome == CheckOutcome.PASS

    def test_el_reason_lleva_el_valor_estimado(self) -> None:
        estimate = _estimate_for(_long_decision())
        reason = check_slippage_estimate(estimate).reason
        assert str(estimate.estimated_slippage_usdt) in reason
        assert ESTIMATION_METHOD in reason

    def test_el_reason_sin_estimacion_lo_dice_explicitamente(self) -> None:
        reason = check_slippage_estimate(None).reason
        assert "Sin estimación de slippage pre-trade" in reason

    def test_una_estimacion_enorme_sigue_sin_bloquear(self) -> None:
        # Notional absurdo: el check no juzga la magnitud, sólo la registra.
        estimate = _estimate_for(_long_decision(), margin_usdt=_D("10"), leverage=10)
        assert check_slippage_estimate(estimate).outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# validate() — el estimado llega a la auditoría en todos los caminos de salida
# ---------------------------------------------------------------------------


class TestValidateRegistraElSlippage:
    def test_approve_incluye_el_slippage_en_reasons(self) -> None:
        decision = _long_decision()
        estimate = _estimate_for(decision)
        result = _validate_neutral_funding(
            aggregation=_aggregation(decision),
            decision=decision,
            daily_loss_usdt=_D("0"),
            total_loss_usdt=_D("0"),
            config=_config(),
            slippage_estimate=estimate,
        )
        assert result.decision == RiskDecision.APPROVE
        assert "slippage_estimate" in result.reasons
        assert str(estimate.estimated_slippage_usdt) in result.reasons["slippage_estimate"]

    def test_block_tambien_incluye_el_slippage_en_reasons(self) -> None:
        # Un trade rechazado también tiene que dejar su estimado auditado:
        # es el dato que permite revisar después por qué se descartó.
        decision = _long_decision()
        estimate = _estimate_for(decision)
        result = _validate_neutral_funding(
            aggregation=_aggregation(decision),
            decision=decision,
            daily_loss_usdt=_D("100"),  # dispara check_daily_drawdown
            total_loss_usdt=_D("100"),
            config=_config(),
            slippage_estimate=estimate,
        )
        assert result.decision == RiskDecision.BLOCK
        assert "slippage_estimate" in result.reasons

    def test_el_slippage_nunca_convierte_un_approve_en_block(self) -> None:
        decision = _long_decision()
        sin_estimado = _validate_neutral_funding(
            aggregation=_aggregation(decision),
            decision=decision,
            daily_loss_usdt=_D("0"),
            total_loss_usdt=_D("0"),
            config=_config(),
        )
        con_estimado = _validate_neutral_funding(
            aggregation=_aggregation(decision),
            decision=decision,
            daily_loss_usdt=_D("0"),
            total_loss_usdt=_D("0"),
            config=_config(),
            slippage_estimate=_estimate_for(decision, margin_usdt=_D("10"), leverage=10),
        )
        assert sin_estimado.decision == con_estimado.decision == RiskDecision.APPROVE

    def test_sin_estimado_la_ausencia_queda_registrada(self) -> None:
        decision = _long_decision()
        result = _validate_neutral_funding(
            aggregation=_aggregation(decision),
            decision=decision,
            daily_loss_usdt=_D("0"),
            total_loss_usdt=_D("0"),
            config=_config(),
        )
        assert "Sin estimación" in result.reasons["slippage_estimate"]


# ---------------------------------------------------------------------------
# estimate_for_decision — mapeo decisión → orden
# ---------------------------------------------------------------------------


class TestEstimateForDecision:
    def test_long_market_estima_media_horquilla_mas_impacto(self) -> None:
        # cantidad = 25 USDT / 95 000 ; coste/unidad = 10 + 95 000×2/10 000 = 29
        estimate = _estimate_for(_long_decision())
        expected = (_D("25") / _D("95000") * _D("29")).quantize(_D("0.00000001"))
        assert estimate.estimated_slippage_usdt == expected

    def test_long_espera_llenar_por_encima(self) -> None:
        estimate = _estimate_for(_long_decision())
        assert estimate.expected_fill_price > _D(str(_ENTRY_PRICE))

    def test_short_espera_llenar_por_debajo(self) -> None:
        decision = _long_decision(decision="SHORT", stop_loss=99000.0, take_profit=88000.0)
        estimate = _estimate_for(decision)
        assert estimate.expected_fill_price < _D(str(_ENTRY_PRICE))

    def test_entrada_limit_no_estima_slippage(self) -> None:
        estimate = _estimate_for(_long_decision(entry_type="LIMIT"))
        assert estimate.estimated_slippage_usdt == _D("0")

    def test_escala_con_el_margen_aprobado(self) -> None:
        decision = _long_decision()
        propuesto = _estimate_for(decision, margin_usdt=_D("10"))
        ajustado = _estimate_for(decision, margin_usdt=_D("5"))
        # Tras un ADJUST_DOWN el notional cae a la mitad y el estimado con él:
        # por eso el cycle_runner re-estima antes de persistir.
        assert propuesto.estimated_slippage_usdt == ajustado.estimated_slippage_usdt * 2

    def test_snapshot_de_otro_simbolo_es_error(self) -> None:
        with pytest.raises(ValueError, match="libro de otro par"):
            _estimate_for(_long_decision(), snapshot=_snapshot(symbol="ETHUSDT"))

    def test_decision_no_ejecutable_es_error(self) -> None:
        # Una NO_OPERAR admite margin_usdt=0 y entry_price=0 por schema: no hay
        # notional que estimar. Estimar igual reventaba el replay entero, que
        # calcula antes de validar y ve decisiones no ejecutables de rutina.
        decision = _long_decision(decision="NO_OPERAR", execute=False, margin_usdt=0.0)
        with pytest.raises(ValueError, match="no describe una orden estimable"):
            _estimate_for(decision, margin_usdt=_D("0"))

    def test_entry_type_no_entry_es_error(self) -> None:
        # Mapear NO_ENTRY a LIMIT daría un estimado de 0 que parece "orden que no
        # cruza el spread", cuando en realidad no hay orden. Hoy es inalcanzable
        # (el Execution Engine rechaza NO_ENTRY), pero nada en el schema ata
        # execute=True a entry_type != NO_ENTRY.
        decision = _long_decision().model_copy(update={"entry_type": "NO_ENTRY"})
        with pytest.raises(ValueError, match="no describe una orden estimable"):
            _estimate_for(decision)


# ---------------------------------------------------------------------------
# is_estimable — el filtro que usan los call sites
# ---------------------------------------------------------------------------


class TestIsEstimable:
    def test_decision_ejecutable_es_estimable(self) -> None:
        assert is_estimable(_long_decision()) is True

    def test_no_operar_no_es_estimable(self) -> None:
        decision = _long_decision(decision="NO_OPERAR", execute=False, margin_usdt=0.0)
        assert is_estimable(decision) is False

    def test_no_entry_no_es_estimable(self) -> None:
        # `execute=True` con `entry_type=NO_ENTRY` no está prohibido por el
        # schema. Antes reventaba la estimación y, con ella, el ciclo del
        # símbolo o la ventana entera del replay.
        decision = _long_decision().model_copy(update={"entry_type": "NO_ENTRY"})
        assert is_estimable(decision) is False


class TestHalfSpread:
    def test_es_medio_spread(self) -> None:
        assert half_spread(_D("99.90"), _D("100.10")) == _D("0.10")

    def test_rechaza_libro_invertido(self) -> None:
        with pytest.raises(ValueError, match="debe ser menor que ask"):
            half_spread(_D("100.10"), _D("99.90"))

    def test_rechaza_precios_no_positivos(self) -> None:
        with pytest.raises(ValueError, match="bid y ask"):
            half_spread(_D("0"), _D("100"))
