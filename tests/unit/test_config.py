"""Tests unitarios del config loader (YAML + env override + validaciones al boot)."""

import pytest

from backend.core.config import (
    AppConfig,  # noqa: F401
    ConfigError,
    Environment,
    _apply_env_overrides,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(monkeypatch: pytest.MonkeyPatch, **env_overrides: str) -> AppConfig:
    """Limpia cache y carga config con env vars inyectadas."""
    # Aislar: limpiar cualquier BOT__ o variable relevante del entorno real
    _infra_vars = {"DATABASE_URL", "APP_HOST", "APP_PORT", "I_UNDERSTAND_LIVE_RISK"}
    for key in list(__import__("os").environ.keys()):
        if key.startswith("BOT__") or key in _infra_vars:
            monkeypatch.delenv(key, raising=False)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    # load_config no usa lru_cache directamente, siempre releer
    return load_config()


# ---------------------------------------------------------------------------
# Tests de carga y defaults
# ---------------------------------------------------------------------------


def test_loads_yaml_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    assert cfg.execution.environment == Environment.PAPER
    assert cfg.challenge.max_margin_per_trade_usdt == 10
    assert cfg.trading.margin_type.value == "ISOLATED"
    assert cfg.database.sqlite_allowed is False
    assert cfg.risk.martingale_allowed is False


def test_paper_is_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    assert cfg.execution.environment == Environment.PAPER


def test_allowed_symbols_match_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    expected = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}
    assert set(cfg.trading.allowed_symbols) == expected


def test_leverage_defaults_within_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    assert cfg.leverage.max_leverage_paper <= 10
    assert cfg.leverage.max_leverage_testnet <= 5
    assert cfg.leverage.max_leverage_live_absolute <= 5
    assert cfg.leverage.max_leverage_live_initial <= 3


def test_trailing_defaults_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    pm = cfg.position_management
    assert pm.trailing_default_mode == "ATR"
    assert pm.trailing_default_percent == 0.02
    assert pm.trailing_default_atr_multiplier == 2.5


def test_be_sl_offset_default_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch)
    assert cfg.position_management.be_sl_offset_percent == 0.0015


def test_be_sl_offset_percent_covers_round_trip_taker_fees(monkeypatch: pytest.MonkeyPatch) -> None:
    """be_sl_offset_percent existe para cubrir el fee de entrada + salida de un stop-out
    en break-even (ver F14). Si el taker rate del fee model sube y nadie actualiza este
    default, el break-even vuelve a perder plata en cada stop-out — este test lo detecta.

    Nota: esto valida contra el fee model de PAPER/backtesting (FeeModel.DEFAULT_TAKER_RATE).
    Cuando entre el adapter de BingX (F16/F17) con tasas reales de exchange, este default
    hay que revalidarlo contra esas tasas, que pueden no coincidir con el fee model simulado.
    """
    from backend.backtesting.fee_model import DEFAULT_TAKER_RATE

    cfg = _load(monkeypatch)
    round_trip_taker_rate = float(DEFAULT_TAKER_RATE) * 2
    assert cfg.position_management.be_sl_offset_percent >= round_trip_taker_rate


def test_blocks_invalid_trailing_mode() -> None:
    from backend.core.config import PositionManagementConfig

    with pytest.raises(ConfigError, match="trailing_default_mode"):
        PositionManagementConfig(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="BOGUS",
            trailing_default_percent=0.02,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=1.0,
            be_sl_offset_percent=0.0015,
        )


def test_blocks_fixed_trailing_mode_at_boot() -> None:
    """FIXED es un TrailingMode válido pero sin default de distancia: falla al boot,
    no al abrir la primera posición (ver build_position_config)."""
    from backend.core.config import PositionManagementConfig

    with pytest.raises(ConfigError, match="FIXED"):
        PositionManagementConfig(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="FIXED",
            trailing_default_percent=0.02,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=1.0,
            be_sl_offset_percent=0.0015,
        )


def test_blocks_trailing_percent_out_of_range() -> None:
    from backend.core.config import PositionManagementConfig

    with pytest.raises(ConfigError, match="trailing_default_percent"):
        PositionManagementConfig(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="PERCENT",
            trailing_default_percent=1.5,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=1.0,
            be_sl_offset_percent=0.0015,
        )


def test_blocks_non_positive_be_trigger_atr_multiplier() -> None:
    """be_trigger_delta = atr_1h * multiplicador y PositionConfig lo exige > 0:
    un multiplicador <= 0 falla al boot, no al abrir la primera posición."""
    from backend.core.config import PositionManagementConfig

    with pytest.raises(ConfigError, match="be_trigger_atr_multiplier"):
        PositionManagementConfig(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="ATR",
            trailing_default_percent=0.02,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=0.0,
            be_sl_offset_percent=0.0015,
        )


def test_blocks_be_sl_offset_percent_out_of_range() -> None:
    from backend.core.config import PositionManagementConfig

    with pytest.raises(ConfigError, match="be_sl_offset_percent"):
        PositionManagementConfig(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="ATR",
            trailing_default_percent=0.02,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=1.0,
            be_sl_offset_percent=1.5,
        )


# ---------------------------------------------------------------------------
# Tests de override por env vars
# ---------------------------------------------------------------------------


def test_env_override_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, BOT__EXECUTION__ENVIRONMENT="TESTNET")
    assert cfg.execution.environment == Environment.TESTNET


def test_env_override_max_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, BOT__RISK__MAX_MARGIN_PER_TRADE_USDT="5")
    assert cfg.risk.max_margin_per_trade_usdt == 5


def test_env_override_leverage_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, BOT__LEVERAGE__MAX_LEVERAGE_PAPER="8")
    assert cfg.leverage.max_leverage_paper == 8


def test_env_override_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, DATABASE_URL="postgresql://user:pass@db:5432/bot")
    assert cfg.database_url == "postgresql://user:pass@db:5432/bot"


def test_env_override_bool_false(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, BOT__STORAGE__LOG_ALL_DECISIONS="false")
    assert cfg.storage.log_all_decisions is False


# ---------------------------------------------------------------------------
# Tests de validacion de reglas no negociables
# ---------------------------------------------------------------------------


def test_blocks_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__DATABASE__SQLITE_ALLOWED="true")


def test_blocks_margin_above_10(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__RISK__MAX_MARGIN_PER_TRADE_USDT="11")


def test_blocks_challenge_margin_above_10(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__CHALLENGE__MAX_MARGIN_PER_TRADE_USDT="15")


def test_blocks_leverage_paper_above_10(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__LEVERAGE__MAX_LEVERAGE_PAPER="11")


def test_blocks_leverage_testnet_above_5(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__LEVERAGE__MAX_LEVERAGE_TESTNET="6")


def test_blocks_leverage_live_absolute_above_5(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__LEVERAGE__MAX_LEVERAGE_LIVE_ABSOLUTE="6")


def test_blocks_martingale(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__RISK__MARTINGALE_ALLOWED="true")


def test_blocks_cross_margin_in_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__RISK__CROSS_MARGIN_ALLOWED="true")


def test_blocks_max_positions_above_3(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        _load(monkeypatch, BOT__TRADING__MAX_OPEN_POSITIONS_ALLOWED="4")


def test_blocks_invalid_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        from backend.core.config import TradingConfig

        TradingConfig(
            market="USDT_M_FUTURES",
            preferred_exchange="BINGX",
            secondary_exchange="BINANCE",
            margin_type="ISOLATED",
            position_mode="ONE_WAY",
            allowed_symbols=["BTCUSDT", "DOGEUSDT"],
            max_open_positions=1,
            max_open_positions_allowed=3,
        )


def test_blocks_cross_margin_type(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises((ConfigError, Exception)):
        from backend.core.config import TradingConfig

        TradingConfig(
            market="USDT_M_FUTURES",
            preferred_exchange="BINGX",
            secondary_exchange="BINANCE",
            margin_type="CROSS",
            position_mode="ONE_WAY",
            allowed_symbols=["BTCUSDT"],
            max_open_positions=1,
            max_open_positions_allowed=3,
        )


# ---------------------------------------------------------------------------
# Tests de bloqueo LIVE
# ---------------------------------------------------------------------------


def test_blocks_live_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("I_UNDERSTAND_LIVE_RISK", raising=False)
    with pytest.raises(ConfigError, match="LIVE bloqueado"):
        _load(monkeypatch, BOT__EXECUTION__ENVIRONMENT="LIVE")


def test_live_allowed_with_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(
        monkeypatch,
        BOT__EXECUTION__ENVIRONMENT="LIVE",
        I_UNDERSTAND_LIVE_RISK="YES",
    )
    assert cfg.execution.environment == Environment.LIVE


def test_blocks_live_with_wrong_confirmation_value(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="LIVE bloqueado"):
        _load(
            monkeypatch,
            BOT__EXECUTION__ENVIRONMENT="LIVE",
            I_UNDERSTAND_LIVE_RISK="yes",  # lowercase no vale
        )


# ---------------------------------------------------------------------------
# Tests de _apply_env_overrides
# ---------------------------------------------------------------------------


def test_apply_env_overrides_bool_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT__STORAGE__LOG_ERRORS", "true")
    data: dict = {"storage": {"log_errors": False}}
    result = _apply_env_overrides(data)
    assert result["storage"]["log_errors"] is True


def test_apply_env_overrides_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT__LEVERAGE__MAX_LEVERAGE_PAPER", "7")
    data: dict = {"leverage": {"max_leverage_paper": 10}}
    result = _apply_env_overrides(data)
    assert result["leverage"]["max_leverage_paper"] == 7


def test_apply_env_overrides_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT__RISK__MIN_CONFIDENCE_PAPER", "0.85")
    data: dict = {"risk": {"min_confidence_paper": 0.70}}
    result = _apply_env_overrides(data)
    assert result["risk"]["min_confidence_paper"] == pytest.approx(0.85)
