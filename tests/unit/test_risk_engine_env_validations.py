"""Tests unitarios para check_leverage_cap y leverage_cap_for_env.

Validaciones de leverage por entorno operativo (F9 [71]).
"""

from backend.core.config import Environment, get_config
from backend.risk_engine.checks import CheckOutcome, check_leverage_cap, leverage_cap_for_env


def _cfg():
    return get_config()


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

    def test_live_returns_absolute_cap(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_live_absolute
        assert leverage_cap_for_env(cfg, Environment.LIVE) == cap


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
# LIVE — cap absoluto 5x (configuración por defecto)
# ---------------------------------------------------------------------------


class TestLiveLeverageCap:
    def test_at_absolute_cap_passes(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_live_absolute
        result = check_leverage_cap(cap, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.PASS

    def test_below_cap_passes(self) -> None:
        cfg = _cfg()
        assert check_leverage_cap(1, cfg, Environment.LIVE).outcome == CheckOutcome.PASS

    def test_above_absolute_cap_triggers_adjust_down(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_live_absolute
        result = check_leverage_cap(cap + 1, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "leverage_cap"
        assert "LIVE" in result.reason
