"""Tests unitarios para check_leverage_cap y leverage_cap_for_env.

Validaciones de leverage por entorno operativo (F9 [71]).
"""

import pytest

from backend.core.config import (
    AppConfig,
    ConfigError,
    Environment,
    LeverageConfig,
    LivePhase,
    get_config,
)
from backend.risk_engine.checks import CheckOutcome, check_leverage_cap, leverage_cap_for_env


def _cfg():
    return get_config()


def _cfg_with_live_phase(phase: LivePhase) -> AppConfig:
    cfg = get_config()
    return cfg.model_copy(
        update={"leverage": cfg.leverage.model_copy(update={"live_phase": phase})}
    )


# ---------------------------------------------------------------------------
# leverage_cap_for_env — helper
# ---------------------------------------------------------------------------


class TestLeverageCapForEnv:
    def test_paper_returns_config_cap(self) -> None:
        cfg = _cfg()
        assert leverage_cap_for_env(cfg, Environment.PAPER) == cfg.leverage.max_leverage_paper

    def test_testnet_returns_config_cap(self) -> None:
        cfg = _cfg()
        assert leverage_cap_for_env(cfg, Environment.TESTNET) == cfg.leverage.max_leverage_testnet

    def test_live_returns_initial_cap_by_default(self) -> None:
        cfg = _cfg()
        assert cfg.leverage.live_phase == LivePhase.INITIAL
        cap = cfg.leverage.max_leverage_live_initial
        assert leverage_cap_for_env(cfg, Environment.LIVE) == cap

    def test_live_initial_phase_returns_initial_cap(self) -> None:
        cfg = _cfg_with_live_phase(LivePhase.INITIAL)
        assert leverage_cap_for_env(cfg, Environment.LIVE) == 3

    def test_live_absolute_phase_returns_absolute_cap(self) -> None:
        cfg = _cfg_with_live_phase(LivePhase.ABSOLUTE)
        assert leverage_cap_for_env(cfg, Environment.LIVE) == 5

    @pytest.mark.parametrize("phase", list(LivePhase))
    def test_phase_does_not_affect_paper_or_testnet(self, phase: LivePhase) -> None:
        cfg = _cfg_with_live_phase(phase)
        assert leverage_cap_for_env(cfg, Environment.PAPER) == cfg.leverage.max_leverage_paper
        assert leverage_cap_for_env(cfg, Environment.TESTNET) == cfg.leverage.max_leverage_testnet


# ---------------------------------------------------------------------------
# LeverageConfig.live_phase — flag de fase explícito
# ---------------------------------------------------------------------------


class TestLivePhaseConfig:
    def _base(self) -> dict[str, object]:
        return get_config().leverage.model_dump(exclude={"live_phase"})

    def test_missing_phase_defaults_to_initial(self) -> None:
        assert LeverageConfig(**self._base()).live_phase == LivePhase.INITIAL

    def test_accepts_absolute(self) -> None:
        cfg = LeverageConfig(**self._base(), live_phase="ABSOLUTE")
        assert cfg.live_phase == LivePhase.ABSOLUTE

    def test_invalid_phase_rejected(self) -> None:
        with pytest.raises((ValueError, ConfigError)):
            LeverageConfig(**self._base(), live_phase="PROMOTED")


# ---------------------------------------------------------------------------
# PAPER — cap 10x (configuración por defecto)
# ---------------------------------------------------------------------------


class TestPaperLeverageCap:
    def test_at_cap_passes(self) -> None:
        cfg = _cfg()
        result = check_leverage_cap(10, cfg, Environment.PAPER)
        assert result.outcome == CheckOutcome.PASS

    def test_below_cap_passes(self) -> None:
        cfg = _cfg()
        assert check_leverage_cap(1, cfg, Environment.PAPER).outcome == CheckOutcome.PASS
        assert check_leverage_cap(5, cfg, Environment.PAPER).outcome == CheckOutcome.PASS

    def test_above_cap_triggers_adjust_down(self) -> None:
        cfg = _cfg()
        result = check_leverage_cap(11, cfg, Environment.PAPER)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "leverage_cap"
        assert "11x" in result.reason
        assert "PAPER" in result.reason

    def test_reason_mentions_cap(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_paper
        result = check_leverage_cap(cap + 1, cfg, Environment.PAPER)
        assert str(cap) in result.reason


# ---------------------------------------------------------------------------
# TESTNET — cap 5x (configuración por defecto)
# ---------------------------------------------------------------------------


class TestTestnetLeverageCap:
    def test_at_cap_passes(self) -> None:
        cfg = _cfg()
        result = check_leverage_cap(5, cfg, Environment.TESTNET)
        assert result.outcome == CheckOutcome.PASS

    def test_below_cap_passes(self) -> None:
        cfg = _cfg()
        assert check_leverage_cap(1, cfg, Environment.TESTNET).outcome == CheckOutcome.PASS

    def test_above_cap_triggers_adjust_down(self) -> None:
        cfg = _cfg()
        result = check_leverage_cap(6, cfg, Environment.TESTNET)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "leverage_cap"
        assert "TESTNET" in result.reason

    def test_paper_cap_does_not_pass_testnet(self) -> None:
        cfg = _cfg()
        # 10x pasa PAPER pero no TESTNET (cap 5x)
        result = check_leverage_cap(10, cfg, Environment.TESTNET)
        assert result.outcome == CheckOutcome.ADJUST_DOWN


# ---------------------------------------------------------------------------
# LIVE — cap según fase: INITIAL 3x (default) · ABSOLUTE 5x
# ---------------------------------------------------------------------------


class TestLiveLeverageCap:
    def test_at_absolute_cap_passes(self) -> None:
        cfg = _cfg_with_live_phase(LivePhase.ABSOLUTE)
        cap = cfg.leverage.max_leverage_live_absolute
        result = check_leverage_cap(cap, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.PASS

    @pytest.mark.parametrize("leverage", [4, 5])
    def test_live_initial_above_3x_triggers_adjust_down(self, leverage: int) -> None:
        cfg = _cfg_with_live_phase(LivePhase.INITIAL)
        result = check_leverage_cap(leverage, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert "3x" in result.reason

    def test_live_initial_at_3x_passes(self) -> None:
        cfg = _cfg_with_live_phase(LivePhase.INITIAL)
        assert check_leverage_cap(3, cfg, Environment.LIVE).outcome == CheckOutcome.PASS

    def test_below_cap_passes(self) -> None:
        cfg = _cfg()
        assert check_leverage_cap(1, cfg, Environment.LIVE).outcome == CheckOutcome.PASS

    def test_above_absolute_cap_triggers_adjust_down(self) -> None:
        cfg = _cfg_with_live_phase(LivePhase.ABSOLUTE)
        cap = cfg.leverage.max_leverage_live_absolute
        result = check_leverage_cap(cap + 1, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "leverage_cap"
        assert "LIVE" in result.reason
