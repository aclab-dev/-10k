"""Config loader: carga config.yaml (Anexo A) con override por env vars y validacion al boot."""

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator

APP_VERSION = "0.1.0"

_ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"})
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


class ConfigError(RuntimeError):
    """Violacion de regla no negociable en la configuracion."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Environment(StrEnum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class MarginType(StrEnum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class PositionMode(StrEnum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"


class LeverageMode(StrEnum):
    DYNAMIC_BY_VOLATILITY = "DYNAMIC_BY_VOLATILITY"
    FIXED = "FIXED"


class DatabaseEngine(StrEnum):
    POSTGRESQL = "POSTGRESQL"


# ---------------------------------------------------------------------------
# Seccion: challenge
# ---------------------------------------------------------------------------


class ChallengeConfig(BaseModel):
    mode: str
    initial_balance_usdt: float
    max_margin_per_trade_usdt: float
    daily_loss_limit_percent: float
    total_loss_limit_percent: float
    all_in_enabled: bool
    automatic_full_reinvestment: bool

    @field_validator("max_margin_per_trade_usdt")
    @classmethod
    def margin_le_10(cls, v: float) -> float:
        if v > 10:
            raise ConfigError(f"max_margin_per_trade_usdt={v} supera el limite absoluto de 10 USDT")
        return v


# ---------------------------------------------------------------------------
# Seccion: trading
# ---------------------------------------------------------------------------


class TradingConfig(BaseModel):
    market: str
    preferred_exchange: str
    secondary_exchange: str
    margin_type: MarginType
    position_mode: PositionMode
    allowed_symbols: list[str]
    max_open_positions: int
    max_open_positions_allowed: int

    @field_validator("allowed_symbols")
    @classmethod
    def symbols_in_whitelist(cls, v: list[str]) -> list[str]:
        invalid = set(v) - _ALLOWED_SYMBOLS
        if invalid:
            raise ConfigError(f"Simbolos no permitidos en allowed_symbols: {invalid}")
        return v

    @field_validator("margin_type")
    @classmethod
    def no_cross_margin(cls, v: MarginType) -> MarginType:
        if v == MarginType.CROSS:
            raise ConfigError("Cross margin esta prohibido. Usar ISOLATED obligatoriamente")
        return v

    @field_validator("max_open_positions_allowed")
    @classmethod
    def positions_allowed_le_3(cls, v: int) -> int:
        if v > 3:
            raise ConfigError(f"max_open_positions_allowed={v} supera el maximo permitido de 3")
        return v


# ---------------------------------------------------------------------------
# Seccion: risk
# ---------------------------------------------------------------------------


class RiskConfig(BaseModel):
    max_margin_per_trade_usdt: float
    max_daily_loss_percent: float
    max_total_loss_percent: float
    min_confidence_paper: float
    min_confidence_testnet: float
    min_confidence_live: float
    min_net_risk_reward: float
    min_trade_quality_score: float
    stop_loss_required: bool
    take_profit_required: bool
    trailing_stop_enabled: bool
    break_even_enabled: bool
    partial_close_enabled: bool
    martingale_allowed: bool
    average_down_allowed: bool
    cross_margin_allowed: bool
    kill_switch_on_error: bool

    @field_validator("max_margin_per_trade_usdt")
    @classmethod
    def margin_le_10(cls, v: float) -> float:
        if v > 10:
            raise ConfigError(f"risk.max_margin_per_trade_usdt={v} supera el limite de 10 USDT")
        return v

    @field_validator("martingale_allowed")
    @classmethod
    def no_martingale(cls, v: bool) -> bool:
        if v:
            raise ConfigError("martingale_allowed=true esta prohibido")
        return v

    @field_validator("cross_margin_allowed")
    @classmethod
    def no_cross(cls, v: bool) -> bool:
        if v:
            raise ConfigError("cross_margin_allowed=true esta prohibido")
        return v


# ---------------------------------------------------------------------------
# Seccion: leverage
# ---------------------------------------------------------------------------


class LeverageConfig(BaseModel):
    mode: LeverageMode
    max_leverage_paper: int
    max_leverage_testnet: int
    max_leverage_live_initial: int
    max_leverage_live_absolute: int
    volatility_adjustment_enabled: bool
    liquidation_distance_required: bool

    @model_validator(mode="after")
    def leverage_limits(self) -> "LeverageConfig":
        if self.max_leverage_paper > 10:
            raise ConfigError(
                f"max_leverage_paper={self.max_leverage_paper} supera el maximo de 10x (PAPER)"
            )
        if self.max_leverage_testnet > 5:
            raise ConfigError(
                f"max_leverage_testnet={self.max_leverage_testnet} supera el maximo de 5x (TESTNET)"
            )
        if self.max_leverage_live_absolute > 5:
            raise ConfigError(
                f"max_leverage_live_absolute={self.max_leverage_live_absolute} supera 5x (LIVE)"
            )
        if self.max_leverage_live_initial > 3:
            raise ConfigError(
                f"max_leverage_live_initial={self.max_leverage_live_initial} supera 3x (LIVE)"
            )
        return self


# ---------------------------------------------------------------------------
# Seccion: ai_and_quant
# ---------------------------------------------------------------------------


class AiAndQuantConfig(BaseModel):
    gpt_role: str
    gpt_is_primary_edge: bool
    quant_signals_required: bool
    market_regime_required: bool
    volatility_assessment_required: bool
    feature_engineering_required: bool
    decision_aggregator_required: bool
    historical_replay_required_before_live: bool
    backtesting_required_before_testnet_or_live: bool


# ---------------------------------------------------------------------------
# Seccion: execution
# ---------------------------------------------------------------------------


class ExecutionConfig(BaseModel):
    environment: Environment
    live_requires_env_confirmation: bool
    live_confirmation_env_var: str
    live_confirmation_required_value: str


# ---------------------------------------------------------------------------
# Seccion: database
# ---------------------------------------------------------------------------


class DatabaseConfig(BaseModel):
    engine: DatabaseEngine
    sqlite_allowed: bool

    @field_validator("sqlite_allowed")
    @classmethod
    def no_sqlite(cls, v: bool) -> bool:
        if v:
            raise ConfigError("sqlite_allowed=true esta prohibido. PostgreSQL es obligatorio")
        return v


# ---------------------------------------------------------------------------
# Seccion: storage
# ---------------------------------------------------------------------------


class StorageConfig(BaseModel):
    log_all_decisions: bool
    log_blocked_decisions: bool
    log_market_snapshots: bool
    log_quant_signals: bool
    log_feature_packages: bool
    log_model_requests: bool
    log_model_responses: bool
    log_risk_validations: bool
    log_orders: bool
    log_positions: bool
    log_replays: bool
    log_backtests: bool
    log_errors: bool


# ---------------------------------------------------------------------------
# Secciones: Anexo G
# ---------------------------------------------------------------------------


class SetupRegistryConfig(BaseModel):
    required: bool
    allow_unregistered_setups: bool
    initial_setups: list[str]


class ReconciliationConfig(BaseModel):
    enabled: bool
    run_before_new_entries: bool
    block_on_orphan_orders: bool
    block_on_untracked_positions: bool
    block_on_unconfirmed_protection: bool
    manual_balance_change_policy: str


class IdempotencyConfig(BaseModel):
    required: bool
    require_cycle_id: bool
    require_decision_id: bool
    require_trade_plan_id: bool
    require_client_order_id: bool
    retry_policy: str


class LiquidationSafetyConfig(BaseModel):
    enabled: bool
    min_distance_entry_to_liquidation_percent: float
    min_buffer_stop_to_liquidation_percent: float
    action_if_invalid: str
    block_if_liquidation_unknown: bool
    block_if_stop_after_liquidation: bool


class CapitalManagementConfig(BaseModel):
    bot_withdrawals_allowed: bool
    automatic_profit_withdrawal: bool
    manual_exchange_withdrawals_allowed: bool
    balance_source_of_truth: str
    recalculate_risk_on_balance_change: bool
    affect_open_positions_on_manual_withdrawal: bool


class PositionManagementConfig(BaseModel):
    partial_close_enabled_mvp: bool
    partial_close_enabled_future_phase: bool
    break_even_enabled: bool
    trailing_stop_enabled: bool
    # Defaults del trailing stop configurable (F14). Declarados y validados al boot;
    # el consumidor (wiring del PositionManager a un loop) los mapeará a PositionConfig.
    # trailing_default_mode se valida contra el enum TrailingMode (import lazy en el
    # validador para evitar el ciclo core.config <-> position_manager vía exchange_adapters).
    trailing_default_mode: str
    trailing_default_percent: float
    trailing_default_atr_multiplier: float

    @field_validator("trailing_default_mode")
    @classmethod
    def mode_is_valid(cls, v: str) -> str:
        from backend.position_manager.schemas import TrailingMode

        try:
            TrailingMode(v)
        except ValueError as exc:
            valid = ", ".join(m.value for m in TrailingMode)
            raise ConfigError(f"trailing_default_mode={v!r} inválido. Válidos: {valid}.") from exc
        return v

    @field_validator("trailing_default_percent")
    @classmethod
    def percent_in_open_unit_interval(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ConfigError(f"trailing_default_percent={v} debe estar en (0, 1).")
        return v

    @field_validator("trailing_default_atr_multiplier")
    @classmethod
    def atr_multiplier_positive(cls, v: float) -> float:
        if v <= 0.0:
            raise ConfigError(f"trailing_default_atr_multiplier={v} debe ser > 0.")
        return v


class FailurePolicyConfig(BaseModel):
    gpt_failure_blocks_new_entries: bool
    token_budget_failure_blocks_new_entries: bool
    deterministic_position_management_without_gpt: bool
    exits_do_not_require_gpt_response: bool
    every_position_requires_confirmed_sl_tp: bool


class BacktestingBiasGuardConfig(BaseModel):
    prevent_lookahead_bias: bool
    prevent_data_leakage: bool
    require_out_of_sample: bool
    require_walk_forward: bool
    include_fees: bool
    include_slippage: bool
    include_funding: bool
    include_latency: bool
    include_partial_fills: bool
    require_regime_performance: bool


class TokenBudgetConfig(BaseModel):
    max_tokens_per_request: int
    max_tokens_per_hour: int
    max_tokens_per_24h: int
    alert_at_percent: float

    @field_validator("alert_at_percent")
    @classmethod
    def _alert_percent_bounds(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"alert_at_percent debe estar en (0, 1), recibido: {v}")
        return v


# ---------------------------------------------------------------------------
# Modelo raiz
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    challenge: ChallengeConfig
    trading: TradingConfig
    risk: RiskConfig
    leverage: LeverageConfig
    ai_and_quant: AiAndQuantConfig
    execution: ExecutionConfig
    database: DatabaseConfig
    storage: StorageConfig
    setup_registry: SetupRegistryConfig
    reconciliation: ReconciliationConfig
    idempotency: IdempotencyConfig
    liquidation_safety: LiquidationSafetyConfig
    capital_management: CapitalManagementConfig
    position_management: PositionManagementConfig
    failure_policy: FailurePolicyConfig
    backtesting_bias_guard: BacktestingBiasGuardConfig
    token_budget: TokenBudgetConfig

    # Campos de infra no presentes en el YAML (solo via env vars)
    database_url: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    db_pool_size: int = 5
    db_max_overflow: int = 10


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """
    Aplica overrides de env vars con prefijo BOT__ y separador __.

    Ejemplos:
      BOT__EXECUTION__ENVIRONMENT=TESTNET  -> data["execution"]["environment"] = "TESTNET"
      BOT__LEVERAGE__MAX_LEVERAGE_PAPER=8  -> data["leverage"]["max_leverage_paper"] = 8
    """
    prefix = "BOT__"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix) :].lower().split("__")
        node: dict[str, Any] = data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        leaf = parts[-1]
        # Convertir strings a tipos basicos cuando sea posible
        if value.lower() == "true":
            node[leaf] = True
        elif value.lower() == "false":
            node[leaf] = False
        else:
            try:
                node[leaf] = int(value)
            except ValueError:
                try:
                    node[leaf] = float(value)
                except ValueError:
                    node[leaf] = value

    # Overrides directos de infra (sin prefijo BOT__)
    if env := os.environ.get("DATABASE_URL"):
        data["database_url"] = env
    if env := os.environ.get("APP_HOST"):
        data["app_host"] = env
    if env := os.environ.get("APP_PORT"):
        data["app_port"] = int(env)
    if env := os.environ.get("DB_POOL_SIZE"):
        data["db_pool_size"] = int(env)
    if env := os.environ.get("DB_MAX_OVERFLOW"):
        data["db_max_overflow"] = int(env)

    return data


def _validate_live_confirmation(cfg: AppConfig) -> None:
    """LIVE requiere doble confirmacion obligatoria via env vars."""
    if cfg.execution.environment != Environment.LIVE:
        return
    if not cfg.execution.live_requires_env_confirmation:
        return
    env_var = cfg.execution.live_confirmation_env_var
    required_value = cfg.execution.live_confirmation_required_value
    actual = os.environ.get(env_var, "")
    if actual != required_value:
        raise ConfigError(
            f"LIVE bloqueado: se requiere {env_var}={required_value} en el entorno. "
            f"Valor actual: '{actual}'"
        )


def load_config(config_path: Path = _CONFIG_PATH) -> AppConfig:
    """Carga config.yaml, aplica env overrides y valida reglas al boot."""
    with config_path.open() as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    raw = _apply_env_overrides(raw)
    cfg = AppConfig.model_validate(raw)
    _validate_live_confirmation(cfg)
    return cfg


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()


# Alias de compatibilidad para codigo existente que importa get_settings / Settings
class Settings(BaseModel):
    """Shim minimo para compatibilidad con imports previos."""

    environment: Environment = Environment.PAPER
    app_version: str = APP_VERSION
    database_url: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    db_pool_size: int = 5
    db_max_overflow: int = 10


def get_settings() -> Settings:
    cfg = get_config()
    return Settings(
        environment=cfg.execution.environment,
        app_version=APP_VERSION,
        database_url=cfg.database_url,
        app_host=cfg.app_host,
        app_port=cfg.app_port,
        db_pool_size=cfg.db_pool_size,
        db_max_overflow=cfg.db_max_overflow,
    )
