"""Validador de frescura y coherencia de snapshots de mercado (sección 4.10.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.market_data.schemas import (
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    MarketSnapshot,
)

# ---------------------------------------------------------------------------
# Umbrales
# ---------------------------------------------------------------------------

_STALE_AGE_S: int = 10
_EXPIRED_AGE_S: int = 30
_MIN_CANDLES_PER_TF: int = 3
_MAX_LAST_PRICE_VS_5M_CLOSE_PCT: Decimal = Decimal("2")


# ---------------------------------------------------------------------------
# Excepción
# ---------------------------------------------------------------------------


class SnapshotRejectedError(Exception):
    """El snapshot no supera la validación y no debe entrar al pipeline."""

    def __init__(self, snapshot_id: str, reason: str) -> None:
        self.snapshot_id = snapshot_id
        self.reason = reason
        super().__init__(f"snapshot_id={snapshot_id} rechazado: {reason}")


# ---------------------------------------------------------------------------
# Evaluadores
# ---------------------------------------------------------------------------


def evaluate_freshness(
    snapshot: MarketSnapshot, now: datetime | None = None
) -> DataFreshnessStatus:
    """Devuelve el estado de frescura según la edad de timestamp_utc.

    Nota: si timestamp_utc está en el futuro (clock skew del exchange), age_s
    será negativo y el snapshot se clasificará como FRESH sin aviso adicional.
    El campo clock_skew_ms del snapshot puede usarse para detectar este caso.
    """
    _now = now if now is not None else datetime.now(UTC)
    age_s = (_now - snapshot.timestamp_utc).total_seconds()

    if age_s > _EXPIRED_AGE_S:
        return DataFreshnessStatus.EXPIRED
    if age_s > _STALE_AGE_S:
        return DataFreshnessStatus.STALE
    return DataFreshnessStatus.FRESH


def _check_gaps(candles: Candles) -> list[str]:
    """Detecta gaps por n_candles insuficiente en algún timeframe."""
    issues: list[str] = []
    for tf_name, candle in (
        ("5m", candles.tf_5m),
        ("15m", candles.tf_15m),
        ("1h", candles.tf_1h),
        ("4h", candles.tf_4h),
    ):
        if candle.n_candles < _MIN_CANDLES_PER_TF:
            issues.append(
                f"tf_{tf_name}: n_candles={candle.n_candles} < mínimo={_MIN_CANDLES_PER_TF} (gap)"
            )
    return issues


def _check_price_coherence(snapshot: MarketSnapshot) -> tuple[list[str], list[str]]:
    """Verifica integridad y coherencia del precio respecto al close de la vela 5m.

    Returns:
        (invalid_issues, warning_issues) — close <= 0 es INVALID; desviación excesiva es WARNING.
    """
    close_5m = snapshot.candles.tf_5m.close
    if close_5m <= 0:
        return [f"tf_5m: close={close_5m} inválido (≤ 0)"], []
    deviation_pct = abs(snapshot.last_price - close_5m) / close_5m * 100
    if deviation_pct > _MAX_LAST_PRICE_VS_5M_CLOSE_PCT:
        return [], [
            f"last_price={snapshot.last_price} se desvía {deviation_pct:.2f}% "
            f"del close_5m={close_5m} (máx={_MAX_LAST_PRICE_VS_5M_CLOSE_PCT}%)"
        ]
    return [], []


def evaluate_coherence(snapshot: MarketSnapshot) -> tuple[CoherenceStatus, list[str]]:
    """
    Evalúa la coherencia del snapshot más allá de lo que valida Pydantic.

    Returns:
        (status, issues) — issues está vacío si status es OK.
    """
    gap_issues = _check_gaps(snapshot.candles)
    price_invalid, price_warnings = _check_price_coherence(snapshot)
    invalid_issues = gap_issues + price_invalid
    all_issues = invalid_issues + price_warnings

    if not all_issues:
        return CoherenceStatus.OK, []

    if invalid_issues:
        return CoherenceStatus.INVALID, all_issues

    return CoherenceStatus.WARNING, all_issues


# ---------------------------------------------------------------------------
# Gate principal
# ---------------------------------------------------------------------------


def validate_snapshot(
    snapshot: MarketSnapshot, now: datetime | None = None
) -> MarketSnapshot:
    """
    Puerta de validación completa. Rechaza snapshots EXPIRED o con coherencia INVALID.

    Args:
        snapshot: El snapshot a validar.
        now: Timestamp de referencia para frescura (inyectable en tests).

    Returns:
        El mismo snapshot si pasa la validación.

    Raises:
        SnapshotRejectedError: Si el snapshot está expirado o tiene datos incoherentes.
    """
    freshness = evaluate_freshness(snapshot, now=now)
    coherence, issues = evaluate_coherence(snapshot)

    expired = freshness == DataFreshnessStatus.EXPIRED
    invalid = coherence == CoherenceStatus.INVALID

    if expired or invalid:
        reasons: list[str] = []
        if expired:
            reasons.append(f"timestamp expirado (age > {_EXPIRED_AGE_S}s)")
        if invalid:
            reasons.append(f"datos incoherentes: {'; '.join(issues)}")
        raise SnapshotRejectedError(
            snapshot_id=snapshot.snapshot_id,
            reason=" | ".join(reasons),
        )

    return snapshot
