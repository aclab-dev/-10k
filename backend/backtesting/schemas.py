"""Contratos Pydantic del Backtesting Engine candle-by-candle (F12 [90]).

Jerarquía de datos:
  CandleRow         → unidad atómica de entrada (OHLCV + funding_rate)
  TradeSignal       → decisión del SignalProvider para el próximo candle
  DatasetSplit      → par in-sample / out-of-sample para validación
  WalkForwardFold   → un fold de walk-forward validation
  OpenPosition      → posición activa en simulación
  ClosedTrade       → trade completado con desglose de costos
  BacktestConfig    → configuración del engine
  BacktestRunResult → resultado final con métricas agregadas
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class CandleRow(BaseModel):
    """OHLCV de un único candle — la unidad atómica que procesa el engine.

    funding_rate: tasa de funding aplicable al cierre de este candle.
    Si no se dispone de datos históricos de funding, dejar en 0.
    """

    timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    funding_rate: Decimal = Decimal("0")

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _ohlc_coherent(self) -> CandleRow:
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open={self.open} fuera del rango [low={self.low}, high={self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"close={self.close} fuera del rango [low={self.low}, high={self.high}]"
            )
        if self.volume < 0:
            raise ValueError("volume no puede ser negativo")
        return self


class TradeSignal(BaseModel):
    """Señal devuelta por el SignalProvider para el próximo candle.

    LONG/SHORT: abrir posición nueva (requiere stop_loss y take_profit).
    CLOSE:      cerrar posición activa al open del siguiente candle.
    NO_OP:      no hacer nada.
    """

    action: Literal["LONG", "SHORT", "CLOSE", "NO_OP"]
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    leverage: int = Field(default=1, ge=1, le=10)
    margin_usdt: Decimal = Field(default=Decimal("10"), gt=Decimal("0"))

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _sl_tp_required(self) -> TradeSignal:
        if self.action in ("LONG", "SHORT"):
            if self.stop_loss is None:
                raise ValueError("stop_loss es obligatorio para señales LONG/SHORT")
            if self.take_profit is None:
                raise ValueError("take_profit es obligatorio para señales LONG/SHORT")
        return self


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------


class OpenPosition(BaseModel):
    """Posición activa en la simulación, a la espera de un evento de cierre."""

    position_id: str = Field(default_factory=_new_id)
    side: Literal["LONG", "SHORT"]
    entry_candle_index: int
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    leverage: int
    margin_usdt: Decimal
    notional_usdt: Decimal
    quantity: Decimal
    entry_fee_usdt: Decimal
    entry_slippage_usdt: Decimal
    accrued_funding_usdt: Decimal = Decimal("0")

    model_config = {"frozen": True}


class ClosedTrade(BaseModel):
    """Trade completado con desglose completo de costos y resultado."""

    trade_id: str = Field(default_factory=_new_id)
    side: Literal["LONG", "SHORT"]
    entry_candle_index: int
    exit_candle_index: int
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: Literal["SL", "TP", "CLOSE_SIGNAL", "END_OF_DATA"]
    leverage: int
    margin_usdt: Decimal
    notional_usdt: Decimal
    gross_pnl_usdt: Decimal
    entry_fee_usdt: Decimal
    exit_fee_usdt: Decimal
    entry_slippage_usdt: Decimal
    exit_slippage_usdt: Decimal
    funding_cost_usdt: Decimal
    net_pnl_usdt: Decimal
    hold_candles: int

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Engine config
# ---------------------------------------------------------------------------


class BacktestConfig(BaseModel):
    """Configuración del BacktestingEngine.

    symbol y timeframe son metadata; el engine no filtra por símbolo.
    latency_candles: retardo adicional sobre el retardo base de 1 candle.
    """

    symbol: str
    timeframe: str
    initial_balance_usdt: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    latency_candles: int = Field(default=0, ge=0)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class BacktestRunResult(BaseModel):
    """Resultado completo de una ejecución de backtesting."""

    symbol: str
    timeframe: str
    candles_processed: int
    trades: list[ClosedTrade]

    # Métricas
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float | None
    profit_factor: float | None
    expectancy_usdt: Decimal | None
    max_drawdown_pct: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None

    # Costos agregados
    total_gross_pnl: Decimal
    total_fees_paid: Decimal
    total_slippage_cost: Decimal
    total_funding_cost: Decimal
    total_net_pnl: Decimal
    final_balance_usdt: Decimal

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Validation helpers (anti-lookahead / data-snooping / overfitting)
# ---------------------------------------------------------------------------


class DatasetSplit(BaseModel):
    """Par in-sample / out-of-sample de candles para validación anti-overfitting."""

    train: tuple[CandleRow, ...]
    test: tuple[CandleRow, ...]

    model_config = {"frozen": True, "arbitrary_types_allowed": True}


class WalkForwardFold(BaseModel):
    """Un fold de walk-forward validation: ventana de entrenamiento + ventana de evaluación."""

    fold_index: int
    train: tuple[CandleRow, ...]
    test: tuple[CandleRow, ...]

    model_config = {"frozen": True, "arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Run results por ventana (F12 [93])
# ---------------------------------------------------------------------------


class SplitBacktestResult(BaseModel):
    """Resultados separados de un backtest IS / OOS.

    in_sample: resultado del engine corriendo sobre el conjunto de entrenamiento.
    out_of_sample: resultado del engine corriendo sobre el conjunto de evaluación.
    train_ratio: fracción de candles usada como IS (para trazabilidad).
    """

    in_sample: BacktestRunResult
    out_of_sample: BacktestRunResult
    train_ratio: Annotated[float, Field(gt=0, lt=1)]

    model_config = {"frozen": True}


class WalkForwardFoldResult(BaseModel):
    """Resultado del engine para un único fold de walk-forward."""

    fold_index: int
    train_result: BacktestRunResult
    test_result: BacktestRunResult

    model_config = {"frozen": True}


class WalkForwardResult(BaseModel):
    """Resultados separados por ventana de un walk-forward backtest.

    fold_results: lista ordenada por fold_index con train_result y test_result de cada fold.
    n_folds: número de folds efectivamente ejecutados; puede diferir del parámetro
        `n_folds` solicitado si los candles son insuficientes para generar todos.
    min_train_candles: mínimo de candles de entrenamiento configurado.
    """

    fold_results: tuple[WalkForwardFoldResult, ...]
    n_folds: int
    min_train_candles: int

    model_config = {"frozen": True}
