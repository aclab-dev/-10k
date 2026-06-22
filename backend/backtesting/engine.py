"""Backtesting Engine candle-by-candle — F12 [90].

Procesa una secuencia de candles OHLCV en orden cronológico, sin lookahead,
produciendo resultados reproducibles.

Garantías:
- No lookahead: el SignalProvider recibe solo candles[0:i] al procesar el candle i.
  El candle actual y los futuros nunca son visibles para el provider.
- Reproducibilidad: mismos candles + mismo SignalProvider → mismo BacktestRunResult.
- Una posición abierta a la vez (MVP: max_open_positions = 1).

Flujo por candle i:
  1. Fill de entrada pendiente (si i >= fill_candle_index).
  2. Check SL/TP intra-candle (si hay posición abierta).
  3. Aplicar funding acumulado al close del candle.
  4. Llamar al SignalProvider con history = candles[0:i] (inmutable, sin candle i).
  5. Encolar nueva entrada o marcar cierre según la señal.

Al final de los datos: cierra posición abierta al close del último candle.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Protocol

import structlog

from backend.backtesting.fee_model import FeeModel
from backend.backtesting.latency_model import LatencyModel
from backend.backtesting.metrics import compute_backtest_metrics
from backend.backtesting.schemas import (
    BacktestConfig,
    BacktestRunResult,
    CandleRow,
    ClosedTrade,
    OpenPosition,
    TradeSignal,
)
from backend.backtesting.slippage_model import SlippageModel

_log = structlog.get_logger(__name__)
_QUANT = Decimal("0.00000001")
_ZERO = Decimal("0")


class SignalProvider(Protocol):
    """Protocolo del proveedor de señales para el BacktestingEngine.

    Recibe el índice del candle actual y el historial inmutable de candles
    anteriores (sin el candle actual — no lookahead). Devuelve un TradeSignal.
    """

    def __call__(
        self,
        candle_index: int,
        candles_history: tuple[CandleRow, ...],
    ) -> TradeSignal: ...


class BacktestingEngine:
    """Engine de backtesting candle-by-candle.

    Procesa candles en orden cronológico. El SignalProvider decide cuándo
    abrir/cerrar posiciones basándose únicamente en el historial pasado.

    Args:
        config: configuración del run (símbolo, timeframe, balance inicial, latencia).
        fee_model: modelo de fees (por defecto: taker 0.05%, maker 0.02%).
        slippage_model: modelo de slippage (por defecto: 2 BPS en MARKET/STOP).
        latency_model: modelo de latencia extra (por defecto: 0 candles adicionales).
    """

    def __init__(
        self,
        config: BacktestConfig,
        fee_model: FeeModel | None = None,
        slippage_model: SlippageModel | None = None,
        latency_model: LatencyModel | None = None,
    ) -> None:
        self._config = config
        self._fee = fee_model or FeeModel()
        self._slip = slippage_model or SlippageModel()
        self._latency = latency_model or LatencyModel(extra_candles=config.latency_candles)

    def run(
        self,
        candles: list[CandleRow],
        signal_provider: SignalProvider,
    ) -> BacktestRunResult:
        """Ejecuta el backtest completo sobre la lista de candles.

        Args:
            candles: candles OHLCV en orden cronológico ascendente.
            signal_provider: proveedor de señales (Protocol SignalProvider).

        Returns:
            BacktestRunResult con la lista de trades cerrados y métricas agregadas.
        """
        if not candles:
            return self._empty_result()

        closed_trades: list[ClosedTrade] = []
        open_pos: OpenPosition | None = None

        # Señal pendiente de fill: (TradeSignal, candle_index_del_fill)
        pending_entry: tuple[TradeSignal, int] | None = None
        # Índice del candle donde hay que cerrar la posición abierta (señal CLOSE)
        pending_close_index: int | None = None

        history: tuple[CandleRow, ...] = ()

        for i, candle in enumerate(candles):
            # ------------------------------------------------------------------
            # 1. Fill de entrada pendiente
            # ------------------------------------------------------------------
            if pending_entry is not None:
                signal, fill_idx = pending_entry
                if i >= fill_idx and open_pos is None:
                    open_pos = self._open_position(signal, candle, i)
                    pending_entry = None
                    _log.debug(
                        "backtest.entry_filled",
                        candle_index=i,
                        side=open_pos.side,
                        entry_price=str(open_pos.entry_price),
                    )

            # ------------------------------------------------------------------
            # 2. Cierre solicitado por señal CLOSE
            # ------------------------------------------------------------------
            if pending_close_index is not None and open_pos is not None:
                if i >= pending_close_index:
                    trade = self._close_position(
                        open_pos,
                        close_price=candle.open,
                        exit_candle_index=i,
                        exit_reason="CLOSE_SIGNAL",
                        close_candle=candle,
                        is_market=True,
                    )
                    closed_trades.append(trade)
                    open_pos = None
                    pending_close_index = None

            # ------------------------------------------------------------------
            # 3. Check SL/TP intra-candle (si hay posición abierta)
            # ------------------------------------------------------------------
            if open_pos is not None:
                sl_tp_result = self._check_sl_tp(open_pos, candle, i)
                if sl_tp_result is not None:
                    closed_trades.append(sl_tp_result)
                    open_pos = None

            # ------------------------------------------------------------------
            # 4. Acumular funding si la posición sigue abierta
            # ------------------------------------------------------------------
            if open_pos is not None and candle.funding_rate != _ZERO:
                funding_payment = self._compute_funding(open_pos, candle)
                open_pos = open_pos.model_copy(
                    update={"accrued_funding_usdt": open_pos.accrued_funding_usdt + funding_payment}
                )

            # ------------------------------------------------------------------
            # 5. Llamar al SignalProvider con history = candles[0:i] (sin candle i)
            # ------------------------------------------------------------------
            signal = signal_provider(i, history)

            if signal.action in ("LONG", "SHORT") and open_pos is None and pending_entry is None:
                fill_idx = i + self._latency.fill_candle_offset()
                pending_entry = (signal, fill_idx)

            elif signal.action == "CLOSE" and open_pos is not None and pending_close_index is None:
                pending_close_index = i + self._latency.fill_candle_offset()

            # Actualizar history: el candle i ya está cerrado y es visible para el siguiente
            history = (*history, candle)

        # ----------------------------------------------------------------------
        # Fin de datos: cerrar posición abierta al close del último candle
        # ----------------------------------------------------------------------
        if open_pos is not None:
            last = candles[-1]
            trade = self._close_position(
                open_pos,
                close_price=last.close,
                exit_candle_index=len(candles) - 1,
                exit_reason="END_OF_DATA",
                close_candle=last,
                is_market=False,  # close al precio de cierre, sin slippage adicional
            )
            closed_trades.append(trade)

        return compute_backtest_metrics(
            trades=closed_trades,
            symbol=self._config.symbol,
            timeframe=self._config.timeframe,
            candles_processed=len(candles),
            initial_balance_usdt=self._config.initial_balance_usdt,
        )

    # --------------------------------------------------------------------------
    # Helpers internos
    # --------------------------------------------------------------------------

    def _open_position(
        self,
        signal: TradeSignal,
        candle: CandleRow,
        candle_index: int,
    ) -> OpenPosition:
        """Abre una posición simulada al open del candle con slippage de MARKET order."""
        assert signal.stop_loss is not None
        assert signal.take_profit is not None

        side_str = "BUY" if signal.action == "LONG" else "SELL"
        fill_price = self._slip.apply(candle.open, side_str, "MARKET")  # type: ignore[arg-type]
        notional = (signal.margin_usdt * Decimal(signal.leverage)).quantize(_QUANT)
        quantity = (notional / fill_price).quantize(_QUANT)
        fee = self._fee.calculate(notional, "MARKET")
        slippage_cost = abs(fill_price - candle.open) * quantity

        return OpenPosition(
            position_id=str(uuid.uuid4()),
            side=signal.action,  # type: ignore[arg-type]
            entry_candle_index=candle_index,
            entry_price=fill_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            leverage=signal.leverage,
            margin_usdt=signal.margin_usdt,
            notional_usdt=notional,
            quantity=quantity,
            entry_fee_usdt=fee,
            entry_slippage_usdt=slippage_cost.quantize(_QUANT),
        )

    def _check_sl_tp(
        self,
        pos: OpenPosition,
        candle: CandleRow,
        candle_index: int,
    ) -> ClosedTrade | None:
        """Verifica si el candle toca SL o TP.

        Regla conservadora: si ambos se tocan en el mismo candle, gana el SL.
        Precio de fill:
          - SL: precio exacto del stop_loss (puede existir gap adverso, aquí ignorado).
          - TP: precio exacto del take_profit.
        """
        sl_hit = candle.low <= pos.stop_loss if pos.side == "LONG" else candle.high >= pos.stop_loss
        tp_hit = (
            candle.high >= pos.take_profit if pos.side == "LONG" else candle.low <= pos.take_profit
        )

        if sl_hit:
            return self._close_position(
                pos,
                close_price=pos.stop_loss,
                exit_candle_index=candle_index,
                exit_reason="SL",
                close_candle=candle,
                is_market=True,
            )
        if tp_hit:
            return self._close_position(
                pos,
                close_price=pos.take_profit,
                exit_candle_index=candle_index,
                exit_reason="TP",
                close_candle=candle,
                is_market=False,  # TP es limit-like: sin slippage adicional
            )
        return None

    def _close_position(
        self,
        pos: OpenPosition,
        close_price: Decimal,
        exit_candle_index: int,
        exit_reason: str,
        close_candle: CandleRow,
        is_market: bool,
    ) -> ClosedTrade:
        """Cierra la posición y calcula el PnL neto."""
        side_str = "SELL" if pos.side == "LONG" else "BUY"
        order_type = "MARKET" if is_market else "LIMIT"

        if is_market:
            exit_fill = self._slip.apply(close_price, side_str, "MARKET")  # type: ignore[arg-type]
        else:
            exit_fill = close_price

        exit_slippage = abs(exit_fill - close_price) * pos.quantity

        exit_fee = self._fee.calculate(
            (exit_fill * pos.quantity).quantize(_QUANT),
            order_type,  # type: ignore[arg-type]
        )

        # Gross PnL en USDT
        if pos.side == "LONG":
            gross_pnl = ((exit_fill - pos.entry_price) * pos.quantity).quantize(_QUANT)
        else:
            gross_pnl = ((pos.entry_price - exit_fill) * pos.quantity).quantize(_QUANT)

        # Aplicar funding pendiente en el candle de cierre si aplica
        funding_at_close = _ZERO
        if close_candle.funding_rate != _ZERO:
            funding_at_close = self._compute_funding(pos, close_candle)

        total_funding = pos.accrued_funding_usdt + funding_at_close

        net_pnl = (
            gross_pnl
            - pos.entry_fee_usdt
            - exit_fee
            - pos.entry_slippage_usdt
            - exit_slippage.quantize(_QUANT)
            - total_funding
        ).quantize(_QUANT)

        return ClosedTrade(
            trade_id=str(uuid.uuid4()),
            side=pos.side,
            entry_candle_index=pos.entry_candle_index,
            exit_candle_index=exit_candle_index,
            entry_price=pos.entry_price,
            exit_price=exit_fill,
            exit_reason=exit_reason,  # type: ignore[arg-type]
            leverage=pos.leverage,
            margin_usdt=pos.margin_usdt,
            notional_usdt=pos.notional_usdt,
            gross_pnl_usdt=gross_pnl,
            entry_fee_usdt=pos.entry_fee_usdt,
            exit_fee_usdt=exit_fee,
            entry_slippage_usdt=pos.entry_slippage_usdt,
            exit_slippage_usdt=exit_slippage.quantize(_QUANT),
            funding_cost_usdt=total_funding.quantize(_QUANT),
            net_pnl_usdt=net_pnl,
            hold_candles=exit_candle_index - pos.entry_candle_index,
        )

    def _compute_funding(self, pos: OpenPosition, candle: CandleRow) -> Decimal:
        """Calcula el pago de funding para este candle.

        Positivo → nosotros pagamos. Negativo → recibimos.
        El signo respeta la convención de futuros perpetuos:
          - Rate positivo + LONG: pagamos.
          - Rate positivo + SHORT: recibimos.
        """
        notional = pos.quantity * candle.close
        if pos.side == "LONG":
            return (notional * candle.funding_rate).quantize(_QUANT)
        return -(notional * candle.funding_rate).quantize(_QUANT)

    def _empty_result(self) -> BacktestRunResult:
        return compute_backtest_metrics(
            trades=[],
            symbol=self._config.symbol,
            timeframe=self._config.timeframe,
            candles_processed=0,
            initial_balance_usdt=self._config.initial_balance_usdt,
        )
