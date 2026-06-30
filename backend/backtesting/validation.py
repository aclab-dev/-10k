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
    """Lanza TypeError si el objeto no es una tuple.

    Verifica que el contenedor sea una tuple inmutable. Asume que CandleRow es
    un modelo Pydantic frozen (frozen=True), por lo que la inmutabilidad del
    contenido está garantizada por el tipo, no por esta función.

    El engine pasa la history como tuple[CandleRow, ...]. Llamar esta función
    desde un SignalProvider o test confirma que el engine no pasó una lista mutable.
    """
    if not isinstance(history, tuple):
        raise TypeError(
            f"history debe ser tuple inmutable, recibido {type(history).__name__!r}. "
            "El engine garantiza no-lookahead solo cuando no se muta la historia."
        )


# ---------------------------------------------------------------------------
# Anti-data-snooping
# ---------------------------------------------------------------------------


def assert_no_parameter_snooping(
    train_candles: tuple[CandleRow, ...] | list[CandleRow],
    eval_candles: tuple[CandleRow, ...] | list[CandleRow],
) -> None:
    """Lanza ValueError si eval_candles se solapa o precede temporalmente a train_candles.

    Detecta dos formas de data snooping:
    1. Solapamiento de timestamps: algún candle de eval está en train.
    2. Lookahead temporal invertido: train contiene timestamps posteriores al inicio
       de eval (ej. train = candles[100:], eval = candles[:50]).

    Precondición: los timestamps de cada secuencia deben ser únicos dentro de ella.
    Si el data feed puede producir timestamps duplicados (glitches del exchange,
    resampling solapado), esta función puede disparar falsos positivos.

    Args:
        train_candles: candles usados para optimizar / entrenar la estrategia.
        eval_candles: candles usados para evaluar la estrategia final.

    Raises:
        ValueError: si hay solapamiento de timestamps o si train contiene datos
                    posteriores al inicio de eval.
    """
    # Un solo recorrido sobre train_candles para construir el set de timestamps
    # y calcular max_train simultáneamente.
    train_ts = set()
    max_train = None
    for c in train_candles:
        train_ts.add(c.timestamp_utc)
        if max_train is None or c.timestamp_utc > max_train:
            max_train = c.timestamp_utc

    overlap = [c for c in eval_candles if c.timestamp_utc in train_ts]

    if overlap:
        raise ValueError(
            f"Data snooping detectado: {len(overlap)} candle(s) de evaluación "
            f"están presentes en el conjunto de entrenamiento. "
            f"Primero: {overlap[0].timestamp_utc}. "
            "Optimizá los parámetros solo sobre el conjunto de entrenamiento."
        )

    # Cuando max_train == min_eval el timestamp ya fue capturado por train_ts
    # y el check anterior ya lanzó, por lo que la rama == es inalcanzable aquí.
    if max_train is not None and eval_candles:
        min_eval = min(c.timestamp_utc for c in eval_candles)
        if max_train > min_eval:
            raise ValueError(
                f"Lookahead temporal detectado: train contiene datos hasta {max_train}, "
                f"pero eval comienza en {min_eval}. "
                "Train debe terminar antes del inicio de eval."
            )

    _log.debug(
        "assert_no_parameter_snooping.ok",
        train_size=len(train_candles),
        eval_size=len(eval_candles),
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

    # Convertir una sola vez a tuple para que los slices devuelvan tuple directamente
    # sin crear listas intermedias en cada iteración del loop.
    candle_tuple = tuple(candles)
    n = len(candle_tuple)
    min_required = min_train_candles + n_folds
    if n < min_required:
        raise ValueError(
            f"Se necesitan al menos {min_required} candles para {n_folds} folds "
            f"con min_train_candles={min_train_candles}, recibidos {n}"
        )

    # test_pool >= n_folds está garantizado por el guard anterior,
    # por lo que fold_size >= 1 siempre.
    test_pool = n - min_train_candles
    fold_size = test_pool // n_folds

    folds: list[WalkForwardFold] = []
    for i in range(n_folds):
        test_start = min_train_candles + i * fold_size
        # El último fold toma todos los candles restantes para evitar pérdida de datos
        test_end = test_start + fold_size if i < n_folds - 1 else n
        train = candle_tuple[:test_start]
        test = candle_tuple[test_start:test_end]
        folds.append(WalkForwardFold(fold_index=i, train=train, test=test))

    _log.debug(
        "walk_forward_splits",
        total=n,
        n_folds=len(folds),
        fold_size=fold_size,
        min_train_candles=min_train_candles,
    )
    return folds
