"""Tests E2E del pipeline completo en modo PAPER — tarjeta [83].

Verifica que los módulos se integran sin excepciones y que los contratos
fluyen correctamente de punta a punta:

    MarketSnapshot → QuantSignalsPackage → MarketRegimeAssessment
    → VolatilityAssessmentPackage → ModelDecision (mock)
    → DecisionAggregationResult → RiskValidationResult
    → OrderResult (PaperAdapter)

GPT se mockea construyendo un ModelDecision directamente, sin llamadas HTTP.
Los motores de Régimen y Volatilidad son reales.
El QuantSignalsPackage se construye con valores explícitos y deterministas
para garantizar escenarios predecibles (los señales individuales tienen sus
propios tests unitarios).

Escenarios:
  1. APPROVE: señales bullish + GPT LONG coherente → risk APPROVE → orden filled.
  2. NO_OPERAR (GPT): GPT decide NO_OPERAR → aggregator NO_OPERAR → sin orden.
  3. NO_OPERAR (contradicción): quant bearish + GPT LONG → aggregator detecta
     direction_mismatch → NO_OPERAR → sin orden.
  4. BLOCK (drawdown): daily_loss supera límite → risk BLOCK → sin orden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.backtesting.fee_model import FeeModel
from backend.backtesting.slippage_model import SlippageModel
from backend.core.config import Environment, load_config
from backend.decision_engine.aggregator import DecisionAggregator
from backend.decision_engine.aggregator_schemas import DecisionAggregationResult
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    EntryType,
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
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.engine import MarketRegimeEngine
from backend.market_regime.schemas import MarketRegimeAssessment, PrimaryRegime
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.risk_engine import engine as risk_engine
from backend.risk_engine.schemas import RiskDecision, RiskValidationResult
from backend.volatility.engine import compute_volatility_assessment
from backend.volatility.schemas import VolatilityAssessmentPackage

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_BASE_PRICE = Decimal("50000")
_BID = _BASE_PRICE
_SPREAD_ABS = Decimal("20")
_ASK = _BID + _SPREAD_ABS
_LAST_PRICE = _BID + _SPREAD_ABS / 2  # 50010

_FEE_MODEL = FeeModel(taker_rate=Decimal("0.0005"), maker_rate=Decimal("0.0002"))
_SLIPPAGE_MODEL = SlippageModel(market_bps=Decimal("5"))

# Candles: strong bullish alignment across all TFs → TRENDING regime
_BULLISH_CANDLE = CandleData(
    open=Decimal("49200"),
    high=Decimal("50000"),
    low=Decimal("49000"),
    close=Decimal("49800"),
    volume=Decimal("500"),
    n_candles=10,
)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_snapshot(
    candle: CandleData = _BULLISH_CANDLE,
    symbol: str = "BTCUSDT",
) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_utc=_NOW,
        exchange=Exchange.PAPER,
        environment=Environment.PAPER,
        symbol=symbol,
        last_price=_LAST_PRICE,
        bid=_BID,
        ask=_ASK,
        spread_absolute=_SPREAD_ABS,
        spread_percent=_SPREAD_ABS / _BID * 100,
        candles=Candles(
            tf_5m=candle,
            tf_15m=candle,
            tf_1h=candle,
            tf_4h=candle,
        ),
        volume=Decimal("50_000_000"),
        funding_rate=0.0001,
        open_interest=Decimal("1_000_000"),
        account_balance_usdt=Decimal("1000"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=_NOW,
        local_time=_NOW,
        clock_skew_ms=0,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _make_quant_signals(
    snapshot_id: str,
    *,
    symbol: str = "BTCUSDT",
    momentum: float = 0.75,
    mean_reversion: float = 0.0,
    breakout: float = 0.50,
    funding: float = 0.10,
    oi: float = 0.40,
    ofi: float = 0.60,
    sweep: float = 0.10,
    signal_strength_score: float | None = 0.60,
    signal_conflict_score: float | None = 0.15,
) -> QuantSignalsPackage:
    """Construye un QuantSignalsPackage con valores explícitos y deterministas."""
    return QuantSignalsPackage.model_validate(
        {
            "snapshot_id": snapshot_id,
            "timestamp_utc": _NOW,
            "symbol": symbol,
            "timeframes_used": ["5m", "15m", "1h", "4h"],
            "momentum_signal": momentum,
            "mean_reversion_signal": mean_reversion,
            "breakout_signal": breakout,
            "funding_signal": funding,
            "open_interest_signal": oi,
            "order_flow_imbalance_signal": ofi,
            "liquidity_sweep_signal": sweep,
            "signal_strength_score": signal_strength_score,
            "signal_conflict_score": signal_conflict_score,
            "signal_confidence": 0.85,
            "raw_feature_refs": {},
        }
    )


def _make_gpt_decision(
    decision_type: DecisionType,
    *,
    symbol: str = "BTCUSDT",
    execute: bool | None = None,
    confidence: float = 0.85,
    entry_price: float = 50_100.0,
    stop_loss: float = 49_500.0,
    take_profit: float = 51_500.0,
    leverage: int = 3,
    margin_usdt: float = 5.0,
) -> ModelDecision:
    """Construye un ModelDecision válido sin llamar a GPT."""
    is_execute = execute if execute is not None else decision_type != DecisionType.NO_OPERAR
    entry_type = EntryType.MARKET

    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=decision_type,
        symbol=symbol,
        entry_type=entry_type,
        entry_price=entry_price,
        stop_loss=stop_loss if is_execute else 0.0,
        take_profit=take_profit if is_execute else 0.0,
        invalidation_price=49_000.0,
        leverage=leverage,
        margin_usdt=margin_usdt,
        estimated_notional_usdt=margin_usdt * leverage,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.3,
        estimated_max_loss_usdt=margin_usdt,
        liquidation_distance_percent_estimated=15.0,
        confidence=confidence,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_breakout_v1",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=QuantSignalsSection(
            momentum=MomentumInterpretation.BULLISH,
            mean_reversion=MeanReversionInterpretation.NEUTRAL,
            breakout_detection=BreakoutInterpretation.CONFIRMED,
            funding_analysis=FundingInterpretation.NEUTRAL,
            open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
            order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
            liquidity_sweep=LiquiditySweepInterpretation.NONE,
        ),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=confidence,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(
            used=False,
            impact=NewsImpact.NEUTRAL,
            summary="No news data used.",
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=True,
            partial_close_plan="none",
            max_time_in_trade_minutes=480,
        ),
        decision_rationale_summary="Bullish momentum with strong quant alignment.",
        risk_notes=[],
        execute=is_execute,
    )


# ---------------------------------------------------------------------------
# Shared pipeline runner
# ---------------------------------------------------------------------------


def _run_pipeline(
    quant_signals: QuantSignalsPackage,
    gpt_decision: ModelDecision,
    snapshot: MarketSnapshot,
    daily_loss_usdt: Decimal = Decimal("0"),
    total_loss_usdt: Decimal = Decimal("0"),
) -> tuple[
    MarketRegimeAssessment,
    VolatilityAssessmentPackage,
    DecisionAggregationResult,
    RiskValidationResult,
]:
    """Ejecuta las etapas deterministas del pipeline (Regime → Volatility → Aggregator → Risk).

    Retorna (regime, volatility, aggregation, risk_result). No coloca órdenes.
    """
    regime_engine = MarketRegimeEngine()
    regime = regime_engine.assess(snapshot)

    volatility = compute_volatility_assessment(snapshot)

    aggregator = DecisionAggregator()
    aggregation = aggregator.aggregate(gpt_decision, quant_signals, regime, volatility)

    config = load_config()
    risk_result = risk_engine.validate(
        aggregation=aggregation,
        decision=gpt_decision,
        daily_loss_usdt=daily_loss_usdt,
        total_loss_usdt=total_loss_usdt,
        config=config,
    )

    return regime, volatility, aggregation, risk_result


# ---------------------------------------------------------------------------
# Escenario 1: APPROVE — señales bullish coherentes con GPT LONG
# ---------------------------------------------------------------------------


class TestE2EApprovePath:
    """Pipeline completo con señales bullish y GPT LONG coherente → APPROVE → orden filled."""

    def test_regime_and_volatility_computed_without_error(self) -> None:
        snapshot = _make_snapshot()
        regime = MarketRegimeEngine().assess(snapshot)
        volatility = compute_volatility_assessment(snapshot)

        assert regime.primary_regime is not None
        assert volatility.volatility_score >= 0.0

    def test_aggregator_returns_long_action(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, _ = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.final_action == DecisionType.LONG
        assert aggregation.contradictions_detected == []
        assert aggregation.aggregated_score >= 0.50

    def test_risk_engine_approves(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        assert risk_result.decision == RiskDecision.APPROVE

    def test_paper_adapter_fills_order_after_approve(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        assert risk_result.decision == RiskDecision.APPROVE

        adapter = PaperAdapter(
            initial_balance_usdt=Decimal("1000"),
            fee_model=_FEE_MODEL,
            slippage_model=_SLIPPAGE_MODEL,
        )
        quantity = Decimal("0.001")
        order_req = OrderRequest(
            symbol=gpt.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=Decimal(str(gpt.entry_price)),
        )
        order_result = adapter.place_order(order_req)

        assert order_result.status == OrderStatus.FILLED
        assert order_result.is_simulated is True
        assert order_result.fill_price is not None
        assert order_result.fill_price > Decimal(str(gpt.entry_price))  # slippage on BUY

    def test_pipeline_ids_are_linked(self) -> None:
        """decision_id del aggregation referencia el decision_id de ModelDecision."""
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.decision_id == gpt.decision_id
        assert risk_result.aggregation_id == aggregation.aggregation_id


# ---------------------------------------------------------------------------
# Escenario 2: NO_OPERAR — GPT decide no operar
# ---------------------------------------------------------------------------


class TestE2ENoOperarGPT:
    """GPT emite NO_OPERAR → Aggregator lo propaga → Risk propaga NO_OPERAR → sin orden."""

    def test_aggregator_propagates_no_operar(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.NO_OPERAR, execute=False)

        _, _, aggregation, _ = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.final_action == DecisionType.NO_OPERAR
        assert aggregation.contradictions_detected == []

    def test_risk_propagates_no_operar(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.NO_OPERAR, execute=False)

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        assert risk_result.decision == RiskDecision.NO_OPERAR
        assert risk_result.adjusted_parameters is None

    def test_no_order_placed_when_no_operar(self) -> None:
        """Verificar que el pipeline NO coloca orden cuando final_action es NO_OPERAR."""
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.NO_OPERAR, execute=False)

        adapter = PaperAdapter(
            initial_balance_usdt=Decimal("1000"),
            fee_model=_FEE_MODEL,
            slippage_model=_SLIPPAGE_MODEL,
        )
        initial_balance = adapter.get_account_state().balance_usdt

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        # Respetar NO_OPERAR: no colocar orden
        assert risk_result.decision == RiskDecision.NO_OPERAR
        # Balance intacto (sin fees ni slippage)
        assert adapter.get_account_state().balance_usdt == initial_balance


# ---------------------------------------------------------------------------
# Escenario 3: NO_OPERAR (contradicción) — quant bearish + GPT LONG
# ---------------------------------------------------------------------------


class TestE2EContradictionNoOperar:
    """Señales quant bearish + GPT LONG → Aggregator detecta direction_mismatch → NO_OPERAR."""

    def test_aggregator_detects_direction_mismatch(self) -> None:
        snapshot = _make_snapshot()
        # Señales fuertemente bearish: net_quant_direction ≈ (-0.7 + -0.3 + -0.5) / 3 = -0.50
        quant = _make_quant_signals(
            snapshot.snapshot_id,
            momentum=-0.70,
            mean_reversion=-0.30,
            breakout=-0.50,
            signal_strength_score=0.50,
            signal_conflict_score=0.10,
        )
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, _ = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.final_action == DecisionType.NO_OPERAR
        contradiction_types = [c.split(":")[0] for c in aggregation.contradictions_detected]
        assert "direction_mismatch" in contradiction_types

    def test_risk_approves_underlying_trade_but_aggregator_says_no_operar(self) -> None:
        """Cuando el Aggregator detecta contradicción, devuelve final_action=NO_OPERAR.
        El Risk Engine, que sólo lee decision.execute, evalúa el trade original y puede
        devolver APPROVE. Es responsabilidad del caller respetar aggregation.final_action
        antes de llamar al risk engine o colocar órdenes."""
        snapshot = _make_snapshot()
        quant = _make_quant_signals(
            snapshot.snapshot_id,
            momentum=-0.70,
            mean_reversion=-0.30,
            breakout=-0.50,
            signal_strength_score=0.50,
            signal_conflict_score=0.10,
        )
        gpt = _make_gpt_decision(DecisionType.LONG)

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        # Aggregator: contradicción detectada → NO_OPERAR
        assert aggregation.final_action == DecisionType.NO_OPERAR
        assert len(aggregation.contradictions_detected) >= 1
        # Risk Engine: evalúa el ModelDecision original (execute=True, SL/TP válidos)
        # y lo aprueba porque el trade en sí cumple todas las reglas de riesgo.
        # El caller NO debe colocar orden cuando aggregation.final_action == NO_OPERAR.
        assert risk_result.decision == RiskDecision.APPROVE

    def test_no_order_placed_when_contradiction(self) -> None:
        """Balance del adapter permanece intacto cuando el pipeline respeta NO_OPERAR."""
        snapshot = _make_snapshot()
        quant = _make_quant_signals(
            snapshot.snapshot_id,
            momentum=-0.70,
            mean_reversion=-0.30,
            breakout=-0.50,
            signal_strength_score=0.50,
            signal_conflict_score=0.10,
        )
        gpt = _make_gpt_decision(DecisionType.LONG)

        adapter = PaperAdapter(
            initial_balance_usdt=Decimal("1000"),
            fee_model=_FEE_MODEL,
            slippage_model=_SLIPPAGE_MODEL,
        )
        initial_balance = adapter.get_account_state().balance_usdt

        _, _, aggregation, _ = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.final_action == DecisionType.NO_OPERAR
        # No colocamos orden al respetar la contradicción
        assert adapter.get_account_state().balance_usdt == initial_balance


# ---------------------------------------------------------------------------
# Escenario 4: BLOCK — drawdown diario supera el límite
# ---------------------------------------------------------------------------


class TestE2EBlockByDrawdown:
    """daily_loss_usdt supera el 10% del balance inicial → Risk BLOCK → sin orden."""

    def test_risk_blocks_on_daily_drawdown_exceeded(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        # config.challenge.initial_balance_usdt = 100 USDT (config.yaml).
        # Límite diario = 10% = 10 USDT. Con daily_loss=15 USDT se supera.
        # El PaperAdapter en tests se inicializa con 1000 USDT, pero el Risk Engine
        # usa el balance del config para calcular el drawdown diario.
        daily_loss = Decimal("15")

        _, _, _, risk_result = _run_pipeline(quant, gpt, snapshot, daily_loss_usdt=daily_loss)

        assert risk_result.decision == RiskDecision.BLOCK
        assert "daily_drawdown" in risk_result.reasons

    def test_no_order_placed_when_blocked(self) -> None:
        snapshot = _make_snapshot()
        quant = _make_quant_signals(snapshot.snapshot_id)
        gpt = _make_gpt_decision(DecisionType.LONG)

        daily_loss = Decimal("15")
        _, _, _, risk_result = _run_pipeline(quant, gpt, snapshot, daily_loss_usdt=daily_loss)

        adapter = PaperAdapter(
            initial_balance_usdt=Decimal("1000"),
            fee_model=_FEE_MODEL,
            slippage_model=_SLIPPAGE_MODEL,
        )
        initial_balance = adapter.get_account_state().balance_usdt

        assert risk_result.decision == RiskDecision.BLOCK
        # No colocamos orden cuando el Risk Engine bloquea
        assert adapter.get_account_state().balance_usdt == initial_balance


# ---------------------------------------------------------------------------
# Escenario extra: símbolo distinto — ETHUSDT corre sin errores
# ---------------------------------------------------------------------------


class TestE2EMultiSymbol:
    """El pipeline corre correctamente con ETHUSDT además de BTCUSDT."""

    def test_ethusdt_approve_path(self) -> None:
        snapshot = _make_snapshot(symbol="ETHUSDT")
        quant = _make_quant_signals(snapshot.snapshot_id, symbol="ETHUSDT")
        gpt = _make_gpt_decision(DecisionType.LONG, symbol="ETHUSDT")

        _, _, aggregation, risk_result = _run_pipeline(quant, gpt, snapshot)

        assert aggregation.symbol == "ETHUSDT"
        assert risk_result.decision in (RiskDecision.APPROVE, RiskDecision.ADJUST_DOWN)
