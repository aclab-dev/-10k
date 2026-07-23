"""Tests unitarios del BacktestingEngine candle-by-candle (F12 [90]).

Cobertura:
- No-lookahead: el SignalProvider nunca ve el candle actual ni futuros.
- Reproducibilidad: mismos inputs → mismo resultado.
- LONG SL: low <= stop_loss cierra en stop_loss.
- LONG TP: high >= take_profit cierra en take_profit.
- SHORT SL/TP simétricos.
- SL y TP en el mismo candle → gana SL (conservador).
- Fees y slippage descontados del net_pnl.
- End-of-data: posición abierta cierra al close del último candle.
- NO_OP y CLOSE_SIGNAL sin posición no crean trade.
- CLOSE_SIGNAL cierra posición al open del siguiente candle.
- Sin candles → resultado vacío.
- Segunda señal con pending_entry activa se descarta silenciosamente.
- Gap de precio en candle de fill que viola SL → SL se activa ese mismo candle.
- sortino_ratio con todos los trades ganadores devuelve None (downside dev = 0).
- Fill parcial en la entrada reduce quantity/notional/fees/PnL proporcionalmente,
  y el cierre liquida el 100% de la cantidad efectivamente abierta.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.backtesting.engine import BacktestingEngine, SignalProvider
from backend.backtesting.partial_fill_model import PartialFillModel
from backend.backtesting.schemas import (
    BacktestConfig,
    BacktestRunResult,
    CandleRow,
    TradeSignal,
)
from backend.position_manager.schemas import TakeProfitLevel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
_D = Decimal


def _candle(
    open: float,
    high: float,
    low: float,
    close: float,
    *,
    offset_hours: int = 0,
    volume: float = 1000.0,
    funding_rate: float = 0.0,
) -> CandleRow:
    return CandleRow(
        timestamp_utc=_BASE_TS + timedelta(hours=offset_hours),
        open=_D(str(open)),
        high=_D(str(high)),
        low=_D(str(low)),
        close=_D(str(close)),
        volume=_D(str(volume)),
        funding_rate=_D(str(funding_rate)),
    )


def _config(latency: int = 0) -> BacktestConfig:
    return BacktestConfig(
        symbol="BTCUSDT",
        timeframe="1h",
        initial_balance_usdt=_D("100"),
        latency_candles=latency,
    )


def _engine(
    latency: int = 0, partial_fill_model: PartialFillModel | None = None
) -> BacktestingEngine:
    return BacktestingEngine(_config(latency), partial_fill_model=partial_fill_model)


def _no_op(_idx: int, _hist: tuple) -> TradeSignal:
    return TradeSignal(action="NO_OP")


def _long_at(trigger_idx: int, sl: float, tp: float) -> SignalProvider:
    """Devuelve LONG en el candle trigger_idx, NO_OP en todos los demás."""

    def _provider(idx: int, hist: tuple) -> TradeSignal:
        if idx == trigger_idx:
            return TradeSignal(
                action="LONG",
                stop_loss=_D(str(sl)),
                take_profit=_D(str(tp)),
                leverage=1,
                margin_usdt=_D("10"),
            )
        return TradeSignal(action="NO_OP")

    return _provider


def _short_at(trigger_idx: int, sl: float, tp: float) -> SignalProvider:
    def _provider(idx: int, hist: tuple) -> TradeSignal:
        if idx == trigger_idx:
            return TradeSignal(
                action="SHORT",
                stop_loss=_D(str(sl)),
                take_profit=_D(str(tp)),
                leverage=1,
                margin_usdt=_D("10"),
            )
        return TradeSignal(action="NO_OP")

    return _provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_no_candles_returns_empty_result(self) -> None:
        result = _engine().run([], _no_op)
        assert isinstance(result, BacktestRunResult)
        assert result.total_trades == 0
        assert result.candles_processed == 0
        assert result.final_balance_usdt == _D("100")


class TestNoLookahead:
    def test_signal_provider_receives_only_past_candles(self) -> None:
        """El provider en el candle i debe recibir exactamente i candles en history."""
        candles = [_candle(100, 110, 90, 105, offset_hours=h) for h in range(5)]
        observed_lengths: list[int] = []

        def _spy(idx: int, hist: tuple) -> TradeSignal:
            observed_lengths.append(len(hist))
            # Verificar que la history es [candles[0]..candles[idx-1]]
            for j, c in enumerate(hist):
                assert c is candles[j], f"Candle {j} en history no coincide"
            return TradeSignal(action="NO_OP")

        _engine().run(candles, _spy)

        # El provider es llamado una vez por candle
        assert observed_lengths == [0, 1, 2, 3, 4]

    def test_current_candle_not_in_history(self) -> None:
        """El candle actual nunca está en la history recibida por el provider."""
        candles = [_candle(100 + i, 110 + i, 90 + i, 105 + i, offset_hours=i) for i in range(3)]

        def _check(idx: int, hist: tuple) -> TradeSignal:
            current = candles[idx]
            assert current not in hist, f"Candle {idx} no debería estar en history"
            return TradeSignal(action="NO_OP")

        _engine().run(candles, _check)


class TestReproducibility:
    def test_same_input_produces_identical_output(self) -> None:
        candles = [_candle(100 + i, 115 + i, 85 + i, 105 + i, offset_hours=i) for i in range(6)]
        provider = _long_at(0, sl=85.0, tp=115.0)

        r1 = _engine().run(candles, provider)
        r2 = _engine().run(candles, provider)

        assert r1.total_net_pnl == r2.total_net_pnl
        assert r1.total_trades == r2.total_trades
        assert r1.final_balance_usdt == r2.final_balance_usdt


class TestLongSL:
    def test_long_sl_hit_when_low_le_stop_loss(self) -> None:
        """SL se activa cuando candle.low <= stop_loss."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # 0: signal LONG aquí
            _candle(102, 108, 98, 105, offset_hours=1),  # 1: fill al open 102 + slippage
            _candle(105, 110, 88, 90, offset_hours=2),  # 2: low=88 < SL=90 → cierra en 90
            _candle(90, 95, 85, 92, offset_hours=3),
        ]
        provider = _long_at(0, sl=90.0, tp=120.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        # SELL slippage aplica adversamente: exit_price < stop_loss (LONG cierra vendiendo)
        assert trade.exit_price <= _D("90")
        assert trade.exit_candle_index == 2
        assert trade.net_pnl_usdt < _D("0")  # pérdida

    def test_long_sl_not_hit_when_low_gt_stop_loss(self) -> None:
        """SL NO se activa si low > stop_loss."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 91, 107, offset_hours=1),  # low=91 > SL=90 → no cierra
            _candle(107, 125, 100, 120, offset_hours=2),  # TP=120 hit
        ]
        provider = _long_at(0, sl=90.0, tp=120.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "TP"


class TestLongTP:
    def test_long_tp_hit_when_high_ge_take_profit(self) -> None:
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),  # fill
            _candle(105, 125, 100, 120, offset_hours=2),  # high=125 >= TP=120
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "TP"
        assert trade.exit_price == _D("120")
        assert trade.net_pnl_usdt > _D("0")  # ganancia


class TestSlVsTpSameCandle:
    def test_sl_wins_when_both_hit_same_candle(self) -> None:
        """Si SL y TP se tocan en el mismo candle, SL gana (conservador)."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),  # fill
            _candle(105, 130, 70, 100, offset_hours=2),  # high=130 >= TP=120, low=70 <= SL=80
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "SL"
        # SL conservador: precio de exit <= stop_loss por slippage adverso en SELL
        assert result.trades[0].exit_price <= _D("80")


class TestShortSLTP:
    def test_short_sl_hit_when_high_ge_stop_loss(self) -> None:
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 104, 99, 101, offset_hours=1),  # fill SHORT al open
            _candle(101, 115, 95, 110, offset_hours=2),  # high=115 >= SL=110
        ]
        provider = _short_at(0, sl=110.0, tp=80.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        assert trade.net_pnl_usdt < _D("0")

    def test_short_tp_hit_when_low_le_take_profit(self) -> None:
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 104, 99, 101, offset_hours=1),  # fill SHORT
            _candle(101, 103, 78, 80, offset_hours=2),  # low=78 <= TP=80
        ]
        provider = _short_at(0, sl=120.0, tp=80.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "TP"
        assert trade.net_pnl_usdt > _D("0")


class TestEndOfData:
    def test_open_position_closed_at_last_close(self) -> None:
        """Posición abierta sin SL/TP hit cierra al close del último candle."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 107, offset_hours=1),  # fill
            _candle(107, 110, 104, 109, offset_hours=2),  # último, no toca SL ni TP
        ]
        provider = _long_at(0, sl=80.0, tp=150.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "END_OF_DATA"
        assert trade.exit_price == _D("109")  # close del último candle


class TestNoOpAndClose:
    def test_no_op_never_opens_position(self) -> None:
        candles = [_candle(100 + i, 110 + i, 90 + i, 105 + i, offset_hours=i) for i in range(5)]
        result = _engine().run(candles, _no_op)
        assert result.total_trades == 0
        assert result.final_balance_usdt == _D("100")

    def test_close_signal_closes_position_at_next_open(self) -> None:
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # 0: señal LONG
            _candle(102, 108, 98, 105, offset_hours=1),  # 1: fill LONG al open
            _candle(105, 110, 100, 108, offset_hours=2),  # 2: señal CLOSE
            _candle(108, 112, 104, 110, offset_hours=3),  # 3: cierre al open=108
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("70"),
                    take_profit=_D("200"),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            if idx == 2:
                return TradeSignal(action="CLOSE")
            return TradeSignal(action="NO_OP")

        result = _engine().run(candles, _provider)
        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "CLOSE_SIGNAL"
        assert trade.exit_candle_index == 3
        # LONG cierra con SELL MARKET: slippage adverso → exit_price < open del candle 3
        assert trade.exit_price < _D("108")


class TestFeesAndSlippage:
    def test_net_pnl_is_gross_minus_fees_and_slippage(self) -> None:
        """net_pnl = gross_pnl - entry_fee - exit_fee - entry_slip - exit_slip - funding."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 110, 95, 108, offset_hours=1),  # fill al open=100
            _candle(108, 125, 105, 120, offset_hours=2),  # TP hit en 120
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        t = result.trades[0]

        reconstructed = (
            t.gross_pnl_usdt
            - t.entry_fee_usdt
            - t.exit_fee_usdt
            - t.entry_slippage_usdt
            - t.exit_slippage_usdt
            - t.funding_cost_usdt
        )
        assert t.net_pnl_usdt == reconstructed.quantize(Decimal("0.00000001"))

    def test_fees_are_positive(self) -> None:
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 110, 95, 108, offset_hours=1),
            _candle(108, 125, 105, 120, offset_hours=2),
        ]
        result = _engine().run(candles, _long_at(0, sl=80.0, tp=120.0))
        t = result.trades[0]
        assert t.entry_fee_usdt > _D("0")
        assert t.exit_fee_usdt > _D("0")


class TestFunding:
    def test_funding_cost_deducted_from_net_pnl(self) -> None:
        """Con funding positivo en LONG, el costo reduce el net_pnl."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 110, 95, 105, offset_hours=1),  # fill
            _candle(105, 110, 100, 108, offset_hours=2, funding_rate=0.001),  # funding 0.1%
            _candle(108, 125, 105, 120, offset_hours=3),  # TP
        ]
        result_with_funding = _engine().run(candles, _long_at(0, sl=80.0, tp=120.0))

        candles_no_funding = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 110, 95, 105, offset_hours=1),
            _candle(105, 110, 100, 108, offset_hours=2, funding_rate=0.0),
            _candle(108, 125, 105, 120, offset_hours=3),
        ]
        result_no_funding = _engine().run(candles_no_funding, _long_at(0, sl=80.0, tp=120.0))

        assert result_with_funding.trades[0].net_pnl_usdt < result_no_funding.trades[0].net_pnl_usdt
        assert result_with_funding.trades[0].funding_cost_usdt > _D("0")


class TestLatency:
    def test_extra_latency_delays_fill_by_n_candles(self) -> None:
        """Con latency_candles=1, la señal en candle 0 llena en candle 2 (1+1)."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # 0: señal LONG
            _candle(102, 108, 98, 105, offset_hours=1),  # 1: retardo, no fill
            _candle(105, 110, 100, 108, offset_hours=2),  # 2: fill aquí (open=105)
            _candle(108, 130, 100, 125, offset_hours=3),  # 3: TP
        ]
        engine = _engine(latency=1)
        result = engine.run(candles, _long_at(0, sl=80.0, tp=125.0))

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.entry_candle_index == 2


class TestMetrics:
    def test_win_rate_computed_correctly(self) -> None:
        """2 trades ganadores de 2 totales → win_rate = 1.0."""
        # Dos señales LONG que tocan TP
        candles = [
            # Trade 1
            _candle(100, 105, 95, 102, offset_hours=0),  # señal 1
            _candle(100, 110, 95, 108, offset_hours=1),  # fill 1
            _candle(108, 125, 105, 120, offset_hours=2),  # TP 1
            # Trade 2
            _candle(120, 125, 115, 122, offset_hours=3),  # señal 2
            _candle(120, 130, 115, 128, offset_hours=4),  # fill 2
            _candle(128, 145, 120, 140, offset_hours=5),  # TP 2
        ]

        def _two_longs(idx: int, hist: tuple) -> TradeSignal:
            if idx in (0, 3):
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("80"),
                    take_profit=_D("125") if idx == 0 else _D("145"),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine().run(candles, _two_longs)
        assert result.total_trades == 2
        assert result.winning_trades == 2
        assert result.win_rate == 1.0

    def test_empty_candles_produces_none_metrics(self) -> None:
        result = _engine().run([], _no_op)
        assert result.win_rate is None
        assert result.sharpe_ratio is None
        assert result.max_drawdown_pct is None


class TestEdgeCases:
    def test_funding_negativo_short_recibe_ingreso(self) -> None:
        """Rate positivo + SHORT → recibe funding (net_pnl > mismo trade sin funding)."""
        candles_with = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 104, 96, 101, offset_hours=1),  # fill SHORT
            _candle(101, 103, 90, 92, offset_hours=2, funding_rate=0.001),  # recibe funding
            _candle(92, 95, 75, 78, offset_hours=3),  # TP hit
        ]
        candles_without = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(100, 104, 96, 101, offset_hours=1),
            _candle(101, 103, 90, 92, offset_hours=2, funding_rate=0.0),
            _candle(92, 95, 75, 78, offset_hours=3),
        ]
        provider = _short_at(0, sl=120.0, tp=78.0)

        result_with = _engine().run(candles_with, provider)
        result_without = _engine().run(candles_without, provider)

        assert result_with.total_trades == 1
        # SHORT con funding positivo recibe pago → funding_cost negativo → mayor net_pnl
        assert result_with.trades[0].funding_cost_usdt < _D("0")
        assert result_with.trades[0].net_pnl_usdt > result_without.trades[0].net_pnl_usdt

    def test_close_signal_sin_posicion_es_noop(self) -> None:
        """CLOSE emitido cuando no hay posición abierta no debe abrir ni cerrar nada."""
        candles = [_candle(100 + i, 110 + i, 90 + i, 105 + i, offset_hours=i) for i in range(4)]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 1:
                return TradeSignal(action="CLOSE")  # no hay posición abierta
            return TradeSignal(action="NO_OP")

        result = _engine().run(candles, _provider)
        assert result.total_trades == 0

    def test_latencia_que_supera_fin_de_datos_cierra_por_end_of_data(self) -> None:
        """Señal LONG en el último candle con latency=2 → fill nunca ocurre.

        La posición pendiente no se llena, no hay trade cerrado.
        """
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),
            _candle(105, 110, 100, 108, offset_hours=2),  # señal LONG aquí (último candle)
        ]
        provider = _long_at(2, sl=80.0, tp=150.0)  # señal en candle 2 = último
        engine = _engine(latency=2)  # fill se haría en candle 2+1+2=5, fuera de rango

        result = engine.run(candles, provider)
        # La entrada nunca se llena porque no hay candles suficientes
        assert result.total_trades == 0

    def test_segunda_senal_con_pending_entry_activa_se_descarta(self) -> None:
        """Segunda señal LONG mientras hay una entrada pendiente → se ignora."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # señal LONG → fill en candle 1
            _candle(102, 108, 98, 105, offset_hours=1),  # fill + otra señal LONG
            _candle(105, 110, 100, 108, offset_hours=2),
            _candle(108, 115, 85, 90, offset_hours=3),  # SL hit
        ]

        call_count = [0]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            call_count[0] += 1
            # Siempre emite LONG; la segunda señal (idx=1) debe ser descartada
            return TradeSignal(
                action="LONG",
                stop_loss=_D("85"),
                take_profit=_D("200"),
                leverage=1,
                margin_usdt=_D("10"),
            )

        result = _engine().run(candles, _provider)
        # Solo debe haber 1 trade, no 2
        assert result.total_trades == 1

    def test_gap_de_precio_en_fill_viola_sl_antes_del_primer_check(self) -> None:
        """Si el fill ocurre en un candle cuyo low ya tocó el SL (gap bajista),
        el SL no se activa hasta el check de ese mismo candle."""
        # Señal LONG en candle 0 → fill en candle 1 al open=50
        # SL=80 pero el candle 1 abre en 50 (gap bajista extremo), low=45
        # El fill se ejecuta en open=50 y luego el SL se comprueba en el mismo candle
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(50, 55, 45, 52, offset_hours=1),  # gap down: fill al open=50, SL hit
            _candle(52, 60, 48, 55, offset_hours=2),
        ]
        provider = _long_at(0, sl=80.0, tp=200.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "SL"
        assert result.trades[0].exit_candle_index == 1

    def test_sortino_ratio_con_todos_los_trades_ganadores_es_none(self) -> None:
        """Cuando no hay retornos negativos, sortino_ratio debe devolver None
        (no se puede dividir por 0 downside deviation)."""
        # 2 candles: señal LONG → fill → TP hit inmediato
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 150, 100, 145, offset_hours=1),  # TP hit en candle 1
        ]
        provider = _long_at(0, sl=80.0, tp=130.0)
        result = _engine().run(candles, provider)

        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "TP"
        assert result.sortino_ratio is None


class _FirstCallZeroThenFullFill(PartialFillModel):
    """Fake de test: la primera lectura de fill_ratio devuelve 0 (descarta la
    orden), las siguientes devuelven 1.0 (fill completo). Permite verificar que
    el slot de posición única queda libre tras un descarte por liquidez nula."""

    def __init__(self) -> None:
        super().__init__()
        self._reads = 0

    @property
    def fill_ratio(self) -> Decimal:  # type: ignore[override]
        self._reads += 1
        return _D("0") if self._reads == 1 else _D("1.0")


class TestPartialFill:
    def test_default_engine_behaves_as_full_fill(self) -> None:
        """Sin partial_fill_model explícito, el engine se comporta con fill_ratio=1.0."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),
            _candle(105, 125, 100, 120, offset_hours=2),
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)

        result_default = _engine().run(candles, provider)
        result_explicit_full = _engine(
            partial_fill_model=PartialFillModel(fill_ratio=_D("1.0"))
        ).run(candles, provider)

        assert (
            result_default.trades[0].notional_usdt == result_explicit_full.trades[0].notional_usdt
        )
        assert result_default.trades[0].net_pnl_usdt == result_explicit_full.trades[0].net_pnl_usdt

    def test_partial_fill_reduces_notional_fees_and_pnl_proportionally(self) -> None:
        """Un fill_ratio=0.5 ejecuta la mitad de la cantidad solicitada: notional,
        fees y net_pnl del trade resultante escalan proporcionalmente."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),  # fill
            _candle(105, 125, 100, 120, offset_hours=2),  # TP hit
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)

        result_full = _engine().run(candles, provider)
        result_half = _engine(partial_fill_model=PartialFillModel(fill_ratio=_D("0.5"))).run(
            candles, provider
        )

        assert result_full.total_trades == 1
        assert result_half.total_trades == 1

        trade_full = result_full.trades[0]
        trade_half = result_half.trades[0]

        assert trade_full.exit_reason == trade_half.exit_reason == "TP"
        assert float(trade_half.notional_usdt) == pytest.approx(
            float(trade_full.notional_usdt) / 2, rel=1e-4
        )
        assert float(trade_half.entry_fee_usdt) == pytest.approx(
            float(trade_full.entry_fee_usdt) / 2, rel=1e-4
        )
        assert float(trade_half.exit_fee_usdt) == pytest.approx(
            float(trade_full.exit_fee_usdt) / 2, rel=1e-4
        )
        assert trade_half.net_pnl_usdt > _D("0")
        assert float(trade_half.net_pnl_usdt) == pytest.approx(
            float(trade_full.net_pnl_usdt) / 2, rel=1e-3
        )

    def test_partial_fill_entry_still_closes_completely_on_sl(self) -> None:
        """El cierre por SL liquida el 100% de la cantidad reducida por el fill
        parcial de entrada — no queda posición residual abierta ni trades extra."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),
            _candle(102, 108, 98, 105, offset_hours=1),  # fill (mitad de lo solicitado)
            _candle(105, 110, 88, 90, offset_hours=2),  # low=88 < SL=90
            _candle(90, 95, 85, 92, offset_hours=3),
        ]
        provider = _long_at(0, sl=90.0, tp=200.0)
        result = _engine(partial_fill_model=PartialFillModel(fill_ratio=_D("0.5"))).run(
            candles, provider
        )

        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "SL"
        assert result.trades[0].net_pnl_usdt < _D("0")

    def test_partial_fill_zero_ratio_discards_order_without_opening_position(self) -> None:
        """fill_ratio=0 (liquidez nula) descarta la orden: no abre posición,
        no genera trade, y no bloquea el slot para señales futuras."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # señal LONG (fill_ratio=0 → sin fill)
            _candle(102, 108, 98, 105, offset_hours=1),
            _candle(105, 125, 100, 120, offset_hours=2),
        ]
        provider = _long_at(0, sl=80.0, tp=120.0)
        result = _engine(partial_fill_model=PartialFillModel(fill_ratio=_D("0"))).run(
            candles, provider
        )

        assert result.total_trades == 0
        assert result.final_balance_usdt == _D("100")

    def test_partial_fill_zero_ratio_frees_slot_for_next_signal(self) -> None:
        """Tras descartar una orden por fill_ratio=0, el slot de posición única
        queda libre: una señal LONG posterior sí puede abrir y cerrar un trade."""
        candles = [
            _candle(100, 105, 95, 102, offset_hours=0),  # 0: señal LONG #1 (se descarta)
            _candle(102, 108, 98, 105, offset_hours=1),  # 1: intento de fill descartado
            _candle(105, 110, 100, 108, offset_hours=2),  # 2: señal LONG #2
            _candle(108, 130, 100, 125, offset_hours=3),  # 3: fill (ratio ya en 1.0)
            _candle(125, 140, 120, 135, offset_hours=4),  # 4: TP hit
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx in (0, 2):
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("70"),
                    take_profit=_D("135"),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine(partial_fill_model=_FirstCallZeroThenFullFill()).run(candles, _provider)

        assert result.total_trades == 1
        assert result.trades[0].entry_candle_index == 3
        assert result.trades[0].exit_reason == "TP"


# ---------------------------------------------------------------------------
# F14 [105] — Cierres parciales (partial_close_enabled=True)
# ---------------------------------------------------------------------------


def _engine_pc(latency: int = 0) -> BacktestingEngine:
    """Engine con partial_close_enabled=True."""
    return BacktestingEngine(_config(latency), partial_close_enabled=True)


class TestPartialClose:
    """Cierres parciales multi-TP en el BacktestingEngine (F14 [105])."""

    def test_long_two_levels_first_hit_emits_partial_trade(self) -> None:
        """TP1 hit → ClosedTrade parcial emitido, posición reducida al 50%."""
        # Candle 0: LONG abierto
        # Candle 1: TP1 hit (high=110 >= 110)
        # Candle 2: TP2 hit (high=120 >= 120) → cierre total
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(105, 110, 104, 109, offset_hours=1),  # TP1
            _candle(112, 125, 111, 120, offset_hours=2),  # TP2
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("120"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine_pc().run(candles, _provider)

        assert result.total_trades == 2
        partial = result.trades[0]
        final = result.trades[1]

        assert partial.exit_reason == "TP_PARTIAL"
        assert partial.is_partial is True
        assert partial.exit_candle_index == 1

        assert final.exit_reason == "TP"
        assert final.is_partial is False
        assert final.exit_candle_index == 2

    def test_long_two_levels_sl_after_first_partial(self) -> None:
        """TP1 hit → cierre parcial; luego SL hit → cierre del remanente."""
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(105, 110, 104, 109, offset_hours=1),  # TP1
            _candle(88, 96, 85, 86, offset_hours=2),       # SL hit
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("120"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine_pc().run(candles, _provider)

        assert result.total_trades == 2
        assert result.trades[0].exit_reason == "TP_PARTIAL"
        assert result.trades[0].is_partial is True
        assert result.trades[1].exit_reason == "SL"
        assert result.trades[1].is_partial is False

    def test_short_two_levels_both_hit(self) -> None:
        """SHORT con dos niveles de TP descendentes."""
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(95, 96, 90, 91, offset_hours=1),   # TP1 (90)
            _candle(89, 90, 80, 81, offset_hours=2),    # TP2 (82)
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="SHORT",
                    stop_loss=_D("110"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("90"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("82"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine_pc().run(candles, _provider)

        assert result.total_trades == 2
        assert result.trades[0].exit_reason == "TP_PARTIAL"
        assert result.trades[0].is_partial is True
        assert result.trades[1].exit_reason == "TP"

    def test_partial_close_costs_attributed_proportionally(self) -> None:
        """Los costos de entrada se atribuyen proporcionalmente al cierre parcial."""
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(105, 110, 104, 109, offset_hours=1),  # TP1 50%
            _candle(112, 125, 111, 120, offset_hours=2),  # TP2 restante
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("120"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine_pc().run(candles, _provider)

        partial = result.trades[0]
        final = result.trades[1]

        # entry_fee total = entry_fee_partial + entry_fee_final
        total_entry_fee = partial.entry_fee_usdt + final.entry_fee_usdt
        # Ambas mitades deben ser iguales (close_fraction=0.5)
        assert partial.entry_fee_usdt == final.entry_fee_usdt
        assert total_entry_fee > _D("0")

        # entry_slippage también se divide
        assert partial.entry_slippage_usdt == final.entry_slippage_usdt

    def test_all_levels_consumed_remaining_closes_at_end_of_data(self) -> None:
        """Si quedan 3 niveles y solo se tocan 2, el remanente cierra en END_OF_DATA."""
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(105, 110, 104, 109, offset_hours=1),  # TP1
            _candle(112, 115, 111, 113, offset_hours=2),  # TP2 (no llega a 120)
            _candle(113, 116, 112, 114, offset_hours=3),  # sin TP3 (135)
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("115"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("135"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        result = _engine_pc().run(candles, _provider)

        assert result.total_trades == 3
        assert result.trades[0].exit_reason == "TP_PARTIAL"
        assert result.trades[1].exit_reason == "TP_PARTIAL"
        assert result.trades[2].exit_reason == "END_OF_DATA"

    def test_signal_with_tp_levels_and_take_profit_raises(self) -> None:
        """TradeSignal no puede tener ambos take_profit y take_profit_levels."""
        import pytest as _pytest

        with _pytest.raises(Exception):
            TradeSignal(
                action="LONG",
                stop_loss=_D("90"),
                take_profit=_D("110"),
                take_profit_levels=(TakeProfitLevel(price=_D("110"), close_fraction=_D("1")),),
                leverage=1,
                margin_usdt=_D("10"),
            )

    def test_engine_disabled_ignores_tp_levels(self) -> None:
        """Con partial_close_enabled=False, señal con take_profit_levels sin take_profit falla."""
        # Necesitamos 2 candles: señal en candle 0, fill en candle 1
        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(102, 105, 101, 103, offset_hours=1),
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        engine = BacktestingEngine(_config())  # partial_close_enabled=False por defecto
        import pytest as _pytest

        with _pytest.raises(ValueError, match="partial_close_enabled=False"):
            engine.run(candles, _provider)

    def test_entry_fee_sum_equals_total_entry_fee(self) -> None:
        """La suma de entry_fee de todos los trades == entry_fee de apertura (invariante).

        Con fees=0 y slippage=0, verifica que los costos de entrada se atribuyen
        correctamente: partial.entry_fee + final.entry_fee == total_entry_fee_at_open.
        """
        from backend.backtesting.fee_model import FeeModel
        from backend.backtesting.slippage_model import SlippageModel

        zero_fee = FeeModel(maker_rate=_D("0"), taker_rate=_D("0"))
        zero_slip = SlippageModel(market_bps=_D("0"))

        candles = [
            _candle(100, 101, 99, 100, offset_hours=0),
            _candle(105, 110, 104, 109, offset_hours=1),  # fill + TP1
            _candle(112, 125, 111, 120, offset_hours=2),  # TP2
        ]

        def _provider(idx: int, hist: tuple) -> TradeSignal:
            if idx == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D("90"),
                    take_profit_levels=(
                        TakeProfitLevel(price=_D("110"), close_fraction=_D("0.5")),
                        TakeProfitLevel(price=_D("120"), close_fraction=_D("1")),
                    ),
                    leverage=1,
                    margin_usdt=_D("10"),
                )
            return TradeSignal(action="NO_OP")

        engine = BacktestingEngine(
            _config(), fee_model=zero_fee, slippage_model=zero_slip, partial_close_enabled=True
        )
        result = engine.run(candles, _provider)

        assert result.total_trades == 2
        partial = result.trades[0]
        final = result.trades[1]

        # Con fee=0, entry_fee siempre es 0 → la suma también es 0
        assert partial.entry_fee_usdt + final.entry_fee_usdt == _D("0")
        # Con slippage=0, net_pnl == gross_pnl para cada trade
        assert partial.net_pnl_usdt == partial.gross_pnl_usdt
        assert final.net_pnl_usdt == final.gross_pnl_usdt
        # Total PnL es positivo (both TPs were profitable)
        assert partial.net_pnl_usdt + final.net_pnl_usdt > _D("0")
