"""PromptBuilder — construcción del prompt maestro para GPT Context Evaluator.

Versiona el prompt para que Historical Replay Engine pueda comparar decisiones
bajo distintas versiones. El system prompt define explícitamente el rol de GPT
como evaluador contextual multi-factor: no es el edge principal ni puede ejecutar.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.market_data.schemas import MarketSnapshot
from backend.market_regime.schemas import MarketRegimeAssessment
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.volatility.schemas import VolatilityAssessmentPackage

PROMPT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# System prompt — define rol, restricciones y schema JSON obligatorio
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Eres el GPT Context Evaluator del sistema
AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK.

## Rol
Evaluador contextual multi-factor. Interpretas señales cuantitativas ya
calculadas, detectas contradicciones, evalúas el contexto narrativo y propones
una decisión estructurada. No calculas señales desde cero ni tienes acceso
directo al exchange.

## Restricciones OBLIGATORIAS
- NO eres el edge principal. El edge surge del Quant Signals Engine, Market
  Regime Engine y Volatility Engine. Tu función es interpretar y sintetizar.
- NO tienes autoridad sobre el Risk Engine. Propones; el Risk Engine decide.
- NO puedes ejecutar órdenes directamente. El campo `execute` es una propuesta.
- Tu respuesta debe ser EXCLUSIVAMENTE JSON válido. Cualquier texto fuera del
  JSON invalida la respuesta y bloquea la ejecución del ciclo completo.
- Si el contexto cuantitativo es débil, contradictorio o insuficiente →
  `"decision": "NO_OPERAR"`.
- Nunca sugieras leverage que supere el máximo del entorno activo.
- Nunca sugieras margin_usdt mayor a 10.0 USDT.
- Si confidence < 0.70 → `"decision": "NO_OPERAR"`.
- Si net_risk_reward < 1.5 → `"decision": "NO_OPERAR"`.
- Si quant_signals está incompleto o contradictorio → `"decision": "NO_OPERAR"`.

## Schema JSON requerido
Devuelve exactamente este JSON con todos los campos. No agregues texto antes ni
después. No uses bloques de código markdown. Solo JSON puro.

{
  "decision_id": "<uuid-v4 generado por ti>",
  "challenge_mode": "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK",
  "schema_version": "PROMPT_VERSION_PLACEHOLDER",
  "environment": "<PAPER|TESTNET|LIVE>",
  "timestamp_utc": "<ISO-8601 UTC>",
  "decision": "<LONG|SHORT|NO_OPERAR>",
  "symbol": "<BTCUSDT|ETHUSDT|BNBUSDT|SOLUSDT|XRPUSDT>",
  "market": "USDT_M_FUTURES",
  "exchange_preference": "BINGX",
  "margin_type": "ISOLATED",
  "position_mode": "ONE_WAY",
  "entry_type": "<MARKET|LIMIT|NO_ENTRY>",
  "entry_price": 0.0,
  "stop_loss": 0.0,
  "take_profit": 0.0,
  "invalidation_price": 0.0,
  "leverage": 1,
  "margin_usdt": 0.0,
  "estimated_notional_usdt": 0.0,
  "estimated_entry_fee_usdt": 0.0,
  "estimated_exit_fee_usdt": 0.0,
  "estimated_slippage_usdt": 0.0,
  "estimated_funding_usdt": 0.0,
  "net_risk_reward": 0.0,
  "estimated_max_loss_usdt": 0.0,
  "liquidation_distance_percent_estimated": 0.0,
  "confidence": 0.0,
  "market_regime": "<TRENDING|RANGING|HIGH_VOLATILITY|LOW_VOLATILITY|BREAKOUT|UNCLEAR>",
  "setup_name": "<nombre del setup identificado>",
  "timeframes_used": ["5m", "15m", "1h", "4h"],
  "quant_signals": {
    "momentum": "<BULLISH|BEARISH|NEUTRAL|UNCLEAR>",
    "mean_reversion": "<LONG_BIAS|SHORT_BIAS|NEUTRAL|UNCLEAR>",
    "breakout_detection": "<CONFIRMED|FAILED|WATCH|NONE>",
    "funding_analysis": "<SUPPORTS_TRADE|CONTRADICTS_TRADE|NEUTRAL|UNCLEAR>",
    "open_interest_analysis": "<RISING_WITH_PRICE|RISING_AGAINST_PRICE|FALLING|NEUTRAL|UNCLEAR>",
    "order_flow_imbalance": "<BUY_PRESSURE|SELL_PRESSURE|BALANCED|UNAVAILABLE>",
    "liquidity_sweep": "<BUY_SIDE_SWEEP|SELL_SIDE_SWEEP|NONE|UNCLEAR>"
  },
  "decision_aggregator": {
    "quant_score": 0.0,
    "gpt_context_score": 0.0,
    "risk_quality_score": 0.0,
    "final_trade_quality_score": 0.0,
    "contradictions_detected": []
  },
  "news_context": {
    "used": false,
    "impact": "<SUPPORTS_TRADE|CONTRADICTS_TRADE|NEUTRAL|UNCLEAR>",
    "summary": ""
  },
  "position_management_plan": {
    "use_trailing_stop": true,
    "move_to_break_even": true,
    "partial_close_plan": "",
    "max_time_in_trade_minutes": 0
  },
  "decision_rationale_summary": "<explicación concisa del razonamiento>",
  "risk_notes": [],
  "execute": false
}

## Reglas de coherencia obligatorias
- decision=NO_OPERAR → execute=false, campos de trading pueden ser 0.
- decision=LONG/SHORT + execute=true → stop_loss > 0 y take_profit > 0.
- margin_usdt ≤ 10.0 siempre, sin excepción.
- confidence ∈ [0.0, 1.0].
- execute=true solo si todos los campos de trading son coherentes y completos.
- Contradicciones fuertes → listar en contradictions_detected, bajar confidence.
"""

# Sustituir el placeholder de versión con el valor real
_SYSTEM_PROMPT = _SYSTEM_PROMPT.replace("PROMPT_VERSION_PLACEHOLDER", PROMPT_VERSION)


# ---------------------------------------------------------------------------
# PromptContext — entrada al builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountContext:
    """Estado mínimo de cuenta necesario para construir el prompt."""

    environment: str
    balance_usdt: float
    open_positions_count: int
    daily_drawdown_percent: float
    max_leverage_for_environment: int


@dataclass(frozen=True)
class PromptContext:
    """Agrega todos los inputs que PromptBuilder necesita para un ciclo."""

    snapshot: MarketSnapshot
    quant_signals: QuantSignalsPackage
    regime: MarketRegimeAssessment
    volatility: VolatilityAssessmentPackage
    account: AccountContext
    prompt_version: str = PROMPT_VERSION


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------


class PromptBuilder:
    """Construye el par (system_message, user_message) para GPT Context Evaluator.

    El system_message define rol, restricciones y schema JSON. El user_message
    provee el contexto cuantitativo completo: snapshot, señales, régimen y
    volatilidad. Ambos están versionados via PROMPT_VERSION para auditoría y replay.
    """

    SYSTEM_PROMPT: str = _SYSTEM_PROMPT

    def build(self, ctx: PromptContext) -> tuple[str, str]:
        """Devuelve (system_message, user_message) listos para enviar al modelo."""
        return self.SYSTEM_PROMPT, self._build_user_message(ctx)

    def _build_user_message(self, ctx: PromptContext) -> str:
        sections = [
            self._section_snapshot(ctx.snapshot),
            self._section_account(ctx.account),
            self._section_quant_signals(ctx.quant_signals),
            self._section_regime(ctx.regime),
            self._section_volatility(ctx.volatility),
            self._section_instructions(ctx),
        ]
        return "\n\n".join(sections)

    def _section_snapshot(self, snap: MarketSnapshot) -> str:
        c5 = snap.candles.tf_5m
        c1h = snap.candles.tf_1h
        c4h = snap.candles.tf_4h
        fr = snap.funding_rate if snap.funding_rate is not None else "N/A"
        oi = snap.open_interest if snap.open_interest is not None else "N/A"
        return (
            f"## Snapshot de mercado\n"
            f"- Símbolo: {snap.symbol}\n"
            f"- Precio actual: {snap.last_price} USDT\n"
            f"- Bid: {snap.bid} | Ask: {snap.ask} | Spread: {snap.spread_percent:.4f}%\n"
            f"- Funding rate: {fr}\n"
            f"- Open interest: {oi}\n"
            f"- Volumen: {snap.volume}\n"
            f"- Freshness: {snap.data_freshness_status} | "
            f"Coherencia: {snap.coherence_status}\n"
            f"- Vela 5m  — O:{c5.open} H:{c5.high} L:{c5.low} "
            f"C:{c5.close} V:{c5.volume}\n"
            f"- Vela 1h  — O:{c1h.open} H:{c1h.high} L:{c1h.low} "
            f"C:{c1h.close} V:{c1h.volume}\n"
            f"- Vela 4h  — O:{c4h.open} H:{c4h.high} L:{c4h.low} "
            f"C:{c4h.close} V:{c4h.volume}"
        )

    def _section_account(self, acc: AccountContext) -> str:
        return (
            f"## Estado de cuenta\n"
            f"- Entorno: {acc.environment}\n"
            f"- Balance disponible: {acc.balance_usdt:.2f} USDT\n"
            f"- Posiciones abiertas: {acc.open_positions_count}\n"
            f"- Drawdown diario actual: {acc.daily_drawdown_percent:.2f}%\n"
            f"- Leverage máximo en este entorno: {acc.max_leverage_for_environment}x\n"
            f"- Margen máximo por operación: 10 USDT (límite absoluto)"
        )

    def _section_quant_signals(self, qs: QuantSignalsPackage) -> str:
        lines = [
            "## Señales cuantitativas (Quant Signals Engine)",
            f"- signal_id: {qs.signal_id}",
            f"- Timeframes usados: {', '.join(qs.timeframes_used)}",
            f"- Versión del contrato: {qs.version}",
            "",
            "### Señales individuales [-1.0 = bearish máximo, +1.0 = bullish máximo]",
            f"- Momentum:             {_fmt_signal(qs.momentum_signal)}",
            f"- Mean reversion:       {_fmt_signal(qs.mean_reversion_signal)}",
            f"- Breakout:             {_fmt_signal(qs.breakout_signal)}",
            f"- Funding:              {_fmt_signal(qs.funding_signal)}",
            f"- Open interest:        {_fmt_signal(qs.open_interest_signal)}",
            f"- Order flow imbalance: {_fmt_signal(qs.order_flow_imbalance_signal)}",
            f"- Liquidity sweep:      {_fmt_signal(qs.liquidity_sweep_signal)}",
            "",
            "### Scores agregados [0.0 - 1.0]",
            f"- Signal strength score:  {_fmt_score(qs.signal_strength_score)}",
            f"- Signal conflict score:  {_fmt_score(qs.signal_conflict_score)}",
            f"- Signal confidence:      {_fmt_score(qs.signal_confidence)}",
        ]
        return "\n".join(lines)

    def _section_regime(self, regime: MarketRegimeAssessment) -> str:
        lines = [
            "## Régimen de mercado (Market Regime Engine)",
            f"- regime_id: {regime.regime_id}",
            f"- Régimen primario:     {regime.primary_regime}",
            f"- Régimen secundario:   {regime.secondary_regime or 'N/A'}",
            f"- Confianza del régimen:{regime.regime_confidence:.2f}",
            f"- Estado de volatilidad:{regime.volatility_state}",
            f"- Estado de liquidez:   {regime.liquidity_state}",
            f"- Estado de funding:    {regime.funding_state}",
            f"- Estado de open int.:  {regime.open_interest_state}",
            f"- Alineación tendencia: {regime.trend_alignment}",
        ]
        if regime.notes:
            lines.append(f"- Notas del clasificador: {'; '.join(regime.notes)}")
        return "\n".join(lines)

    def _section_volatility(self, vol: VolatilityAssessmentPackage) -> str:
        return (
            f"## Volatilidad (Volatility Engine)\n"
            f"- assessment_id: {vol.assessment_id}\n"
            f"- Régimen de volatilidad:     {vol.volatility_regime}\n"
            f"- ATR 1h:                     {vol.atr_1h} ({vol.atr_percent:.2f}%)\n"
            f"- ATR 4h:                     {vol.atr_4h}\n"
            f"- Realized vol:               {vol.realized_vol:.4f}\n"
            f"- Volatility score:           {vol.volatility_score:.2f}\n"
            f"- Liquidation risk score:     {vol.liquidation_risk_score:.2f}\n"
            f"- Leverage cap recomendado:   {vol.leverage_cap}x"
        )

    def _section_instructions(self, ctx: PromptContext) -> str:
        return (
            f"## Instrucciones para esta evaluación\n"
            f"- prompt_version: {ctx.prompt_version}\n"
            f"- snapshot_id: {ctx.snapshot.snapshot_id}\n"
            f"- signal_id: {ctx.quant_signals.signal_id}\n"
            f"- regime_id: {ctx.regime.regime_id}\n"
            f"- assessment_id: {ctx.volatility.assessment_id}\n"
            f"\n"
            f"Evalúa el contexto completo y devuelve SOLO el JSON del schema.\n"
            f"Si las señales son contradictorias o insuficientes → NO_OPERAR.\n"
            f"No agregues texto fuera del JSON. No uses markdown. Solo JSON puro."
        )


def _fmt_signal(v: float | None) -> str:
    return f"{v:+.4f}" if v is not None else "N/A"


def _fmt_score(v: float | None) -> str:
    return f"{v:.4f}" if v is not None else "N/A"
