"""Utilidades de validación anti-lookahead, data-snooping y overfitting (F12 [92]).

Reglas implementadas:
1. Anti-lookahead: garantiza que el SignalProvider nunca recibe datos futuros
   (complementa la garantía ya presente en BacktestingEngine con utilidades de
   validación explícitas).
2. Anti-data-snooping: detecta cuando los parámetros de una estrategia se
   optimizaron sobre el mismo conjunto de candles que se usa para evaluarla.
3. Anti-overfitting: split in-sample / out-of-sample y walk-forward validation
   para medir la degradación de métricas en datos no vistos.
"""

from __future__ import annotations

import structlog

from backend.backtesting.schemas import (
    CandleRow,
    DatasetSplit,
    WalkForwardFold,
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Anti-lookahead
# ---------------------------------------------------------------------------


def assert_history_immutable(history: tuple[CandleRow, ...]) -> None:
    """Lanza TypeError si el objeto no es una tuple (garantía de inmutabilidad).

    El engine pasa la history como tuple[CandleRow, ...]. Esta función se puede
    llamar desde tests o desde un SignalProvider para verificar que la historia
    no es mutable.
    """
    if not isinstance(history, tuple):
        raise TypeError(
            f"history debe ser tuple inmutable, recibido {type(history).__name__!r}. "
            "El engine garantiza no-lookahead solo cuando no se muta la historia."
        )


# ---------------------------------------------------------------------------
# Anti-data-snooping
# ---------------------------------------------------------------------------


def detect_parameter_snooping(
    train_candles: tuple[CandleRow, ...] | list[CandleRow],
    eval_candles: tuple[CandleRow, ...] | list[CandleRow],
) -> None:
    """Lanza ValueError si eval_candles se solapa con train_candles.

    El uso correcto es: optimizar parámetros sobre train_candles, evaluar sobre
    eval_candles distintos. Si hay solapamiento se produce data snooping: la
    estrategia "conoce" los datos de evaluación durante la optimización.

    Args:
        train_candles: candles usados para optimizar / entrenar la estrategia.
        eval_candles: candles usados para evaluar la estrategia final.

    Raises:
        ValueError: si algún timestamp de eval_candles está en train_candles.
    """
    train_ts = {c.timestamp_utc for c in train_candles}
    overlap = [c for c in eval_candles if c.timestamp_utc in train_ts]

    if overlap:
        raise ValueError(
            f"Data snooping detectado: {len(overlap)} candle(s) de evaluación "
            f"están presentes en el conjunto de entrenamiento. "
            f"Primero: {overlap[0].timestamp_utc}. "
            "Optimizá los parámetros solo sobre el conjunto de entrenamiento."
        )

    _log.debug(
        "detect_parameter_snooping.ok",
        train_size=len(list(train_candles)),
        eval_size=len(list(eval_candles)),
    )


# ---------------------------------------------------------------------------
# Anti-overfitting: dataset split
# ---------------------------------------------------------------------------


def split_dataset(
    candles: list[CandleRow] | tuple[CandleRow, ...],
    train_ratio: float = 0.7,
) -> DatasetSplit:
    """Divide los candles en in-sample (train) y out-of-sample (test) en orden temporal.

    Args:
        candles: secuencia de candles en orden cronológico.
        train_ratio: fracción a usar como train (0 < train_ratio < 1).

    Returns:
        DatasetSplit con train y test sin solapamiento.

    Raises:
        ValueError: si train_ratio está fuera de (0, 1) o si hay menos de 2 candles.
    """
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio debe estar en (0, 1), recibido {train_ratio}")

    candle_list = list(candles)
    n = len(candle_list)

    if n < 2:
        raise ValueError(f"Se necesitan al menos 2 candles para hacer un split, recibidos {n}")

    split_idx = max(1, min(n - 1, int(n * train_ratio)))
    train = tuple(candle_list[:split_idx])
    test = tuple(candle_list[split_idx:])

    _log.debug("split_dataset", total=n, train=len(train), test=len(test), train_ratio=train_ratio)
    return DatasetSplit(train=train, test=test)


# ---------------------------------------------------------------------------
# Anti-overfitting: walk-forward validation
# ---------------------------------------------------------------------------


def walk_forward_splits(
    candles: list[CandleRow] | tuple[CandleRow, ...],
    n_folds: int = 5,
    min_train_candles: int = 10,
) -> list[WalkForwardFold]:
    """Genera folds de walk-forward validation sin solapamiento entre train y test.

    En walk-forward, cada fold tiene:
    - train: todos los candles anteriores a la ventana de test del fold.
    - test: la ventana de test del fold (no solapada con train).

    Esto simula cómo una estrategia real se reoptimiza periódicamente sin ver
    datos futuros.

    Args:
        candles: secuencia de candles en orden cronológico.
        n_folds: número de folds (ventanas de test).
        min_train_candles: mínimo de candles que debe tener el train del primer fold.

    Returns:
        Lista de WalkForwardFold ordenada cronológicamente.

    Raises:
        ValueError: si los parámetros no permiten generar al menos 1 fold válido.
    """
    if n_folds < 1:
        raise ValueError(f"n_folds debe ser >= 1, recibido {n_folds}")
    if min_train_candles < 1:
        raise ValueError(f"min_train_candles debe ser >= 1, recibido {min_train_candles}")

    candle_list = list(candles)
    n = len(candle_list)
    min_required = min_train_candles + n_folds
    if n < min_required:
        raise ValueError(
            f"Se necesitan al menos {min_required} candles para {n_folds} folds "
            f"con min_train_candles={min_train_candles}, recibidos {n}"
        )

    # Dividir los candles disponibles para test (tras el min_train_candles inicial)
    test_pool = n - min_train_candles
    fold_size = test_pool // n_folds
    if fold_size < 1:
        raise ValueError(
            f"fold_size={fold_size}: no hay suficientes candles para {n_folds} folds. "
            f"Reducí n_folds o min_train_candles."
        )

    folds: list[WalkForwardFold] = []
    for i in range(n_folds):
        test_start = min_train_candles + i * fold_size
        # El último fold toma todos los candles restantes para evitar pérdida de datos
        test_end = test_start + fold_size if i < n_folds - 1 else n
        train = tuple(candle_list[:test_start])
        test = tuple(candle_list[test_start:test_end])
        folds.append(WalkForwardFold(fold_index=i, train=train, test=test))

    _log.debug(
        "walk_forward_splits",
        total=n,
        n_folds=len(folds),
        fold_size=fold_size,
        min_train_candles=min_train_candles,
    )
    return folds
