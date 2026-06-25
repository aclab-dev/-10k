"""Tests explícitos de anti-lookahead, data snooping y overfitting (F12 [92]).

Cobertura:
- assert_history_immutable: acepta tuple, rechaza lista.
- detect_parameter_snooping: permite conjuntos disjuntos, bloquea solapamiento.
- split_dataset: proporciones correctas, orden cronológico, sin solapamiento.
- split_dataset: errores para train_ratio inválido y menos de 2 candles.
- walk_forward_splits: N folds correctos, train crece, test sin solapamiento con train.
- walk_forward_splits: errores para parámetros inválidos.
- Overfitting demo: strategy overfitteada al IS no transfiere al OOS.
- Anti-lookahead integración: history pasada al provider es tuple inmutable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.backtesting.engine import BacktestingEngine
from backend.backtesting.schemas import (
    BacktestConfig,
    CandleRow,
    TradeSignal,
)
from backend.backtesting.validation import (
    assert_history_immutable,
    assert_no_parameter_snooping,
    split_dataset,
    walk_forward_splits,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
_D = Decimal


def _candle(
    close: float,
    *,
    offset_hours: int,
    open: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> CandleRow:
    o = open if open is not None else close
    h = high if high is not None else close * 1.01
    lo = low if low is not None else close * 0.99
    return CandleRow(
        timestamp_utc=_BASE_TS + timedelta(hours=offset_hours),
        open=_D(str(o)),
        high=_D(str(h)),
        low=_D(str(lo)),
        close=_D(str(close)),
        volume=_D("1000"),
        funding_rate=_D("0"),
    )


def _candles(n: int) -> list[CandleRow]:
    return [_candle(100.0 + i, offset_hours=i) for i in range(n)]


# ---------------------------------------------------------------------------
# assert_history_immutable
# ---------------------------------------------------------------------------


class TestAssertHistoryImmutable:
    def test_accepts_tuple(self) -> None:
        history: tuple[CandleRow, ...] = tuple(_candles(3))
        assert_history_immutable(history)  # no debe lanzar

    def test_rejects_list(self) -> None:
        history_list: list[CandleRow] = _candles(3)  # type: ignore[assignment]
        with pytest.raises(TypeError, match="tuple inmutable"):
            assert_history_immutable(history_list)  # type: ignore[arg-type]

    def test_accepts_empty_tuple(self) -> None:
        assert_history_immutable(())


# ---------------------------------------------------------------------------
# detect_parameter_snooping
# ---------------------------------------------------------------------------


class TestAssertNoParameterSnooping:
    def test_no_overlap_is_ok(self) -> None:
        train = _candles(5)
        test = [_candle(200.0, offset_hours=5 + i) for i in range(5)]
        assert_no_parameter_snooping(train, test)  # no debe lanzar

    def test_overlap_raises(self) -> None:
        candles = _candles(10)
        train = candles[:7]
        # test incluye el candle 6 (solapamiento)
        test = candles[6:]
        with pytest.raises(ValueError, match="Data snooping detectado"):
            assert_no_parameter_snooping(train, test)

    def test_full_overlap_raises(self) -> None:
        candles = _candles(5)
        with pytest.raises(ValueError, match="Data snooping detectado"):
            assert_no_parameter_snooping(candles, candles)

    def test_overlap_reports_count(self) -> None:
        candles = _candles(10)
        train = candles[:8]
        test = candles[5:]  # 3 candles solapados (5,6,7)
        with pytest.raises(ValueError, match="3 candle"):
            assert_no_parameter_snooping(train, test)

    def test_accepts_tuples(self) -> None:
        train = tuple(_candles(4))
        test = tuple(_candle(200.0, offset_hours=4 + i) for i in range(4))
        assert_no_parameter_snooping(train, test)  # no debe lanzar


# ---------------------------------------------------------------------------
# split_dataset
# ---------------------------------------------------------------------------


class TestSplitDataset:
    def test_default_ratio_70_30(self) -> None:
        candles = _candles(10)
        split = split_dataset(candles)
        assert len(split.train) == 7
        assert len(split.test) == 3

    def test_train_and_test_cover_all_candles(self) -> None:
        candles = _candles(20)
        split = split_dataset(candles, train_ratio=0.8)
        assert len(split.train) + len(split.test) == 20

    def test_no_temporal_overlap(self) -> None:
        candles = _candles(20)
        split = split_dataset(candles, train_ratio=0.7)
        train_ts = {c.timestamp_utc for c in split.train}
        test_ts = {c.timestamp_utc for c in split.test}
        assert train_ts.isdisjoint(test_ts)

    def test_train_comes_before_test_chronologically(self) -> None:
        candles = _candles(20)
        split = split_dataset(candles, train_ratio=0.6)
        assert split.train[-1].timestamp_utc < split.test[0].timestamp_utc

    def test_result_is_immutable_tuple(self) -> None:
        candles = _candles(10)
        split = split_dataset(candles)
        assert isinstance(split.train, tuple)
        assert isinstance(split.test, tuple)

    def test_invalid_ratio_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="train_ratio"):
            split_dataset(_candles(10), train_ratio=0.0)

    def test_invalid_ratio_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="train_ratio"):
            split_dataset(_candles(10), train_ratio=1.0)

    def test_less_than_two_candles_raises(self) -> None:
        with pytest.raises(ValueError, match="al menos 2 candles"):
            split_dataset(_candles(1))

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="al menos 2 candles"):
            split_dataset([])

    def test_small_dataset_at_least_one_in_each_split(self) -> None:
        candles = _candles(2)
        split = split_dataset(candles, train_ratio=0.5)
        assert len(split.train) >= 1
        assert len(split.test) >= 1


# ---------------------------------------------------------------------------
# walk_forward_splits
# ---------------------------------------------------------------------------


class TestWalkForwardSplits:
    def test_returns_n_folds(self) -> None:
        folds = walk_forward_splits(_candles(50), n_folds=5, min_train_candles=10)
        assert len(folds) == 5

    def test_fold_indices_are_sequential(self) -> None:
        folds = walk_forward_splits(_candles(30), n_folds=3, min_train_candles=5)
        assert [f.fold_index for f in folds] == [0, 1, 2]

    def test_train_grows_each_fold(self) -> None:
        folds = walk_forward_splits(_candles(40), n_folds=4, min_train_candles=5)
        for i in range(1, len(folds)):
            assert len(folds[i].train) > len(folds[i - 1].train)

    def test_no_overlap_between_train_and_test_in_same_fold(self) -> None:
        folds = walk_forward_splits(_candles(50), n_folds=5, min_train_candles=10)
        for fold in folds:
            train_ts = {c.timestamp_utc for c in fold.train}
            test_ts = {c.timestamp_utc for c in fold.test}
            assert train_ts.isdisjoint(test_ts), f"Solapamiento en fold {fold.fold_index}"

    def test_test_windows_do_not_overlap_across_folds(self) -> None:
        folds = walk_forward_splits(_candles(50), n_folds=5, min_train_candles=10)
        seen: set = set()
        for fold in folds:
            test_ts = {c.timestamp_utc for c in fold.test}
            assert test_ts.isdisjoint(seen), f"Ventana de test solapada en fold {fold.fold_index}"
            seen |= test_ts

    def test_all_candles_covered_by_test_windows(self) -> None:
        """Cada candle posterior al min_train aparece en exactamente un test."""
        candles = _candles(30)
        folds = walk_forward_splits(candles, n_folds=4, min_train_candles=6)
        test_ts: set = set()
        for fold in folds:
            test_ts |= {c.timestamp_utc for c in fold.test}
        expected_ts = {c.timestamp_utc for c in candles[6:]}
        assert test_ts == expected_ts

    def test_train_is_always_tuple(self) -> None:
        folds = walk_forward_splits(_candles(20), n_folds=2, min_train_candles=5)
        for fold in folds:
            assert isinstance(fold.train, tuple)
            assert isinstance(fold.test, tuple)

    def test_n_folds_1_boundary(self) -> None:
        """n_folds=1 es el mínimo válido: un único fold que cubre todo el test pool."""
        folds = walk_forward_splits(_candles(15), n_folds=1, min_train_candles=5)
        assert len(folds) == 1
        assert len(folds[0].train) == 5
        assert len(folds[0].test) == 10  # todos los candles restantes

    def test_invalid_n_folds_raises(self) -> None:
        with pytest.raises(ValueError, match="n_folds"):
            walk_forward_splits(_candles(20), n_folds=0)

    def test_invalid_min_train_candles_raises(self) -> None:
        with pytest.raises(ValueError, match="min_train_candles"):
            walk_forward_splits(_candles(20), n_folds=2, min_train_candles=0)

    def test_insufficient_candles_raises(self) -> None:
        # Necesita min_train_candles(10) + n_folds(5) = 15, solo hay 10
        with pytest.raises(ValueError, match="al menos"):
            walk_forward_splits(_candles(10), n_folds=5, min_train_candles=10)


# ---------------------------------------------------------------------------
# Overfitting demo: IS vs OOS degradation
# ---------------------------------------------------------------------------


class TestOverfittingDemo:
    """Demuestra que una estrategia overfitteada al IS no transfiere al OOS.

    La estrategia tiene acceso explícito a los candles de IS para elegir
    exactamente los mejores trades (simulando parameter-fitting extremo).
    En OOS, sin ese conocimiento, los resultados se degradan.
    """

    def _make_engine(self) -> BacktestingEngine:
        config = BacktestConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_balance_usdt=_D("1000"),
            latency_candles=0,
        )
        return BacktestingEngine(config)

    def test_is_outperforms_oos_with_overfitted_strategy(self) -> None:
        """Una strategy que 'memoriza' el IS no genera trades en OOS.

        La estrategia usa `is_candle_set` para decidir cuándo entrar: solo dispara
        en candles cuyo timestamp pertenece al IS. En OOS esos timestamps no existen,
        por lo que `total_trades == 0` es el outcome esperado y correcto — demuestra
        que el conocimiento del IS no transfiere al OOS.
        """
        candles = _candles(40)
        split = split_dataset(candles, train_ratio=0.5)

        is_candle_set = {c.timestamp_utc for c in split.train}

        def overfitted_provider(idx: int, hist: tuple[CandleRow, ...]) -> TradeSignal:
            if not hist:
                return TradeSignal(action="NO_OP")
            last = hist[-1]
            if last.timestamp_utc in is_candle_set and idx % 5 == 0:
                return TradeSignal(
                    action="LONG",
                    stop_loss=_D(str(float(last.close) * 0.50)),
                    take_profit=_D(str(float(last.close) * 1.50)),
                )
            return TradeSignal(action="NO_OP")

        engine = self._make_engine()
        is_result = engine.run(list(split.train), overfitted_provider)
        oos_result = engine.run(list(split.test), overfitted_provider)

        # IS genera trades; OOS no puede porque los timestamps IS no aparecen ahí
        assert is_result.total_trades > 0, "La estrategia debe generar trades en IS"
        assert oos_result.total_trades == 0, (
            "La estrategia overfitteada no debe encontrar señales en OOS "
            "(los timestamps IS no existen en el conjunto de test)"
        )

    def test_walk_forward_train_never_leaks_to_test(self) -> None:
        """Cada fold de walk-forward garantiza aislamiento temporal."""
        candles = _candles(50)
        folds = walk_forward_splits(candles, n_folds=4, min_train_candles=10)

        for fold in folds:
            assert_no_parameter_snooping(fold.train, fold.test)


# ---------------------------------------------------------------------------
# Integración: history del engine es tuple en todo momento
# ---------------------------------------------------------------------------


class TestEngineHistoryIsAlwaysTuple:
    """Verifica que el engine pasa history como tuple (garantía de inmutabilidad)."""

    def test_history_type_is_tuple_on_every_call(self) -> None:
        candles = _candles(5)
        received_types: list[type] = []

        def _spy(idx: int, hist: tuple[CandleRow, ...]) -> TradeSignal:
            received_types.append(type(hist))
            assert_history_immutable(hist)  # lanzaría TypeError si fuera lista
            return TradeSignal(action="NO_OP")

        config = BacktestConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_balance_usdt=_D("100"),
            latency_candles=0,
        )
        BacktestingEngine(config).run(candles, _spy)

        assert all(t is tuple for t in received_types)
        assert len(received_types) == 5

    def test_provider_cannot_modify_history_via_concatenation(self) -> None:
        """Mutar la referencia de history no afecta al engine (tuple es inmutable)."""
        candles = _candles(5)
        original_lengths: list[int] = []

        def _mutating_spy(idx: int, hist: tuple[CandleRow, ...]) -> TradeSignal:
            original_lengths.append(len(hist))
            # Intentar "extender" la history (no altera la tuple original)
            _ = hist + (candles[0],)
            return TradeSignal(action="NO_OP")

        config = BacktestConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_balance_usdt=_D("100"),
            latency_candles=0,
        )
        BacktestingEngine(config).run(candles, _mutating_spy)

        # Los largos recibidos deben ser 0,1,2,3,4 — no se contamina entre llamadas
        assert original_lengths == [0, 1, 2, 3, 4]
