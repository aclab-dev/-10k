"""Contrato MarketSnapshot — sección 4.10.1 del PDF maestro.

Este módulo define el contrato de datos que el Market Data Engine produce y
que todos los módulos downstream (Quant Signals, Volatility, Feature Engineering,
Context Builder) consumen. Es el único punto de entrada de datos de mercado al
pipeline cuantitativo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.core.config import Environment

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ALLOWED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"})
_ALLOWED_SYMBOLS = ALLOWED_SYMBOLS  # alias privado mantenido por compatibilidad interna
_MAX_SPREAD_PERCENT = Decimal("5.0")
_MAX_CLOCK_SKEW_MS = 5_000
_MAX_LATENCY_MS = 10_000


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Exchange(StrEnum):
    BINGX = "BINGX"
    BINANCE = "BINANCE"
    PAPER = "PAPER"


class DataFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


class CoherenceStatus(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    INVALID = "INVALID"


# ---------------------------------------------------------------------------
# Sub-modelos
# ---------------------------------------------------------------------------


class CandleData(BaseModel):
    """OHLCV para un timeframe dado."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    n_candles: int = Field(ge=1)

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def ohlc_coherence(self) -> CandleData:
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open={self.open} fuera del rango [low={self.low}, high={self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"close={self.close} fuera del rango [low={self.low}, high={self.high}]"
            )
        if self.volume < 0:
            raise ValueError("volume no puede ser negativo")
        return self


class Candles(BaseModel):
    """Candles por timeframe requeridos por el contrato 4.10.1."""

    tf_5m: CandleData
    tf_15m: CandleData
    tf_1h: CandleData
    tf_4h: CandleData

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class MarketSnapshot(BaseModel):
    """Contrato completo de un snapshot de mercado (sección 4.10.1).

    Inmutable una vez construido. Todos los módulos downstream deben
    recibir este modelo y no acceder directamente al exchange.
    """

    # Identificación
    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_utc: datetime

    # Exchange / entorno
    exchange: Exchange
    environment: Environment
    symbol: str
    market: Literal["FUTURES"] = "FUTURES"

    # Precios spot
    last_price: Decimal = Field(gt=0)
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    spread_absolute: Decimal = Field(ge=0)
    spread_percent: Decimal = Field(ge=0)

    # Candles multi-timeframe
    candles: Candles

    # Métricas de mercado
    volume: Decimal = Field(ge=0)
    volatility: float | None = None
    funding_rate: float | None = None
    open_interest: Decimal | None = None

    # Estado de cuenta
    account_balance_usdt: Decimal = Field(ge=0)
    open_positions_count: int = Field(ge=0)
    active_orders_count: int = Field(ge=0)

    # Latencia y sincronización
    latency_ms: int = Field(ge=0)
    exchange_server_time: datetime
    local_time: datetime
    clock_skew_ms: int

    # Estado de calidad del dato
    data_freshness_status: DataFreshnessStatus
    coherence_status: CoherenceStatus

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores de campo
    # ------------------------------------------------------------------

    @field_validator("symbol")
    @classmethod
    def symbol_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(_ALLOWED_SYMBOLS)}")
        return v

    @field_validator("timestamp_utc", "exchange_server_time", "local_time")
    @classmethod
    def must_be_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("El timestamp debe incluir timezone (UTC).")
        return v

    @field_validator("clock_skew_ms")
    @classmethod
    def clock_skew_bound(cls, v: int) -> int:
        if abs(v) > _MAX_CLOCK_SKEW_MS:
            raise ValueError(
                f"|clock_skew_ms|={abs(v)} supera el límite de {_MAX_CLOCK_SKEW_MS} ms"
            )
        return v

    @field_validator("latency_ms")
    @classmethod
    def latency_bound(cls, v: int) -> int:
        if v > _MAX_LATENCY_MS:
            raise ValueError(f"latency_ms={v} supera el límite de {_MAX_LATENCY_MS} ms")
        return v

    # ------------------------------------------------------------------
    # Validadores de modelo (cross-field)
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def prices_coherent(self) -> MarketSnapshot:
        if self.bid >= self.ask:
            raise ValueError(f"bid={self.bid} debe ser menor que ask={self.ask}")

        expected_spread = self.ask - self.bid
        if abs(self.spread_absolute - expected_spread) > Decimal("0.0001"):
            raise ValueError(
                f"spread_absolute={self.spread_absolute} inconsistente"
                f" con ask-bid={expected_spread}"
            )

        expected_spread_pct = self.spread_absolute / self.bid * 100
        if abs(self.spread_percent - expected_spread_pct) > Decimal("0.01"):
            raise ValueError(
                f"spread_percent={self.spread_percent} inconsistente con"
                f" spread_absolute/bid*100={expected_spread_pct:.4f}"
            )

        if self.spread_percent > _MAX_SPREAD_PERCENT:
            raise ValueError(
                f"spread_percent={self.spread_percent}% supera el máximo"
                f" tolerable de {_MAX_SPREAD_PERCENT}%"
            )

        if not (self.bid <= self.last_price <= self.ask):
            raise ValueError(
                f"last_price={self.last_price} debe estar entre bid={self.bid} y ask={self.ask}"
            )

        return self

    # ------------------------------------------------------------------
    # Serialización a tabla market_snapshots
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en market_snapshots.

        Los campos sin columna propia en el esquema actual van en `extra`.
        """
        extra: dict[str, Any] = {
            "exchange": self.exchange,
            "environment": self.environment,
            "market": self.market,
            "spread_percent": str(self.spread_percent),
            "volatility": self.volatility,
            "account_balance_usdt": str(self.account_balance_usdt),
            "open_positions_count": self.open_positions_count,
            "active_orders_count": self.active_orders_count,
            "latency_ms": self.latency_ms,
            "exchange_server_time": self.exchange_server_time.isoformat(),
            "local_time": self.local_time.isoformat(),
            "clock_skew_ms": self.clock_skew_ms,
            "data_freshness_status": self.data_freshness_status,
            "coherence_status": self.coherence_status,
            "candles": {
                "tf_5m": self.candles.tf_5m.model_dump(mode="json"),
                "tf_15m": self.candles.tf_15m.model_dump(mode="json"),
                "tf_1h": self.candles.tf_1h.model_dump(mode="json"),
                "tf_4h": self.candles.tf_4h.model_dump(mode="json"),
            },
        }

        return {
            "id": self.snapshot_id,
            "bot_run_id": bot_run_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "open": self.candles.tf_5m.open,
            "high": self.candles.tf_5m.high,
            "low": self.candles.tf_5m.low,
            # close = last_price (precio actual, no el cierre de la vela 5m)
            "close": self.last_price,
            "volume": self.volume,
            "funding_rate": self.funding_rate,
            "open_interest": self.open_interest,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread_absolute,
            "extra": extra,
        }
