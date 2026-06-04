"""Tests unitarios para adjustments.py — compute_adjusted_margin y compute_adjusted_leverage.

Estas funciones son puras y no tienen tests dedicados. Los tests del engine
las ejercitan de forma indirecta, pero aquí se verifican los contratos
exactos de cada función de forma aislada.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import Environment, get_config
from backend.risk_engine.adjustments import compute_adjusted_leverage, compute_adjusted_margin


# ---------------------------------------------------------------------------
# compute_adjusted_margin
# ---------------------------------------------------------------------------


class TestComputeAdjustedMargin:
    """Devuelve min(original, cap): nunca excede el tope y nunca sube."""

    def test_below_cap_returns_original(self) -> None:
        result = compute_adjusted_margin(Decimal("3.0"), Decimal("10.0"))
        assert result == Decimal("3.0")

    def test_at_cap_returns_original(self) -> None:
        result = compute_adjusted_margin(Decimal("10.0"), Decimal("10.0"))
        assert result == Decimal("10.0")

    def test_above_cap_returns_cap(self) -> None:
        result = compute_adjusted_margin(Decimal("8.0"), Decimal("5.0"))
        assert result == Decimal("5.0")

    def test_min_value_one_cent(self) -> None:
        result = compute_adjusted_margin(Decimal("0.01"), Decimal("10.0"))
        assert result == Decimal("0.01")

    def test_cap_smaller_than_original_always_wins(self) -> None:
        for original, cap in [
            (Decimal("10.0"), Decimal("9.99")),
            (Decimal("5.0"), Decimal("1.0")),
            (Decimal("10.0"), Decimal("0.01")),
        ]:
            assert compute_adjusted_margin(original, cap) == cap

    def test_result_is_decimal(self) -> None:
        result = compute_adjusted_margin(Decimal("7.5"), Decimal("10.0"))
        assert isinstance(result, Decimal)

    def test_equal_original_and_cap(self) -> None:
        for val in (Decimal("1.0"), Decimal("5.0"), Decimal("10.0")):
            assert compute_adjusted_margin(val, val) == val

    def test_never_exceeds_cap(self) -> None:
        cases = [
            (Decimal("10.0"), Decimal("10.0")),
            (Decimal("9.99"), Decimal("10.0")),
            (Decimal("10.01"), Decimal("10.0")),
        ]
        for original, cap in cases:
            assert compute_adjusted_margin(original, cap) <= cap


# ---------------------------------------------------------------------------
# compute_adjusted_leverage
# ---------------------------------------------------------------------------


def _cfg():
    return get_config()


class TestComputeAdjustedLeverage:
    """Devuelve min(original, cap_del_entorno): nunca excede el cap y nunca sube."""

    # ---- PAPER (cap 10x por defecto) ----

    def test_paper_below_cap_returns_original(self) -> None:
        cfg = _cfg()
        result = compute_adjusted_leverage(5, cfg, Environment.PAPER)
        assert result == 5

    def test_paper_at_cap_returns_original(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_paper
        result = compute_adjusted_leverage(cap, cfg, Environment.PAPER)
        assert result == cap

    def test_paper_above_cap_returns_cap(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_paper
        result = compute_adjusted_leverage(cap + 1, cfg, Environment.PAPER)
        assert result == cap

    # ---- TESTNET (cap 5x por defecto) ----

    def test_testnet_below_cap_returns_original(self) -> None:
        cfg = _cfg()
        result = compute_adjusted_leverage(3, cfg, Environment.TESTNET)
        assert result == 3

    def test_testnet_at_cap_returns_original(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_testnet
        result = compute_adjusted_leverage(cap, cfg, Environment.TESTNET)
        assert result == cap

    def test_testnet_above_cap_returns_cap(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_testnet
        result = compute_adjusted_leverage(cap + 1, cfg, Environment.TESTNET)
        assert result == cap

    def test_testnet_paper_cap_exceeds_testnet(self) -> None:
        """Un leverage válido para PAPER puede exceder el cap de TESTNET."""
        cfg = _cfg()
        paper_cap = cfg.leverage.max_leverage_paper
        testnet_cap = cfg.leverage.max_leverage_testnet
        # Solo relevante si PAPER es más permisivo que TESTNET
        if paper_cap > testnet_cap:
            result = compute_adjusted_leverage(paper_cap, cfg, Environment.TESTNET)
            assert result == testnet_cap

    # ---- LIVE (cap absoluto 5x por defecto) ----

    def test_live_below_cap_returns_original(self) -> None:
        cfg = _cfg()
        result = compute_adjusted_leverage(1, cfg, Environment.LIVE)
        assert result == 1

    def test_live_at_cap_returns_original(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_live_absolute
        result = compute_adjusted_leverage(cap, cfg, Environment.LIVE)
        assert result == cap

    def test_live_above_cap_returns_cap(self) -> None:
        cfg = _cfg()
        cap = cfg.leverage.max_leverage_live_absolute
        result = compute_adjusted_leverage(cap + 1, cfg, Environment.LIVE)
        assert result == cap

    # ---- Invariantes ----

    def test_result_is_int(self) -> None:
        cfg = _cfg()
        for env in (Environment.PAPER, Environment.TESTNET, Environment.LIVE):
            result = compute_adjusted_leverage(5, cfg, env)
            assert isinstance(result, int)

    def test_result_never_exceeds_cap_for_any_env(self) -> None:
        cfg = _cfg()
        for env in (Environment.PAPER, Environment.TESTNET, Environment.LIVE):
            from backend.risk_engine.checks import leverage_cap_for_env

            cap = leverage_cap_for_env(cfg, env)
            for lev in (1, 3, cap, cap + 1, cap + 5):
                result = compute_adjusted_leverage(lev, cfg, env)
                assert result <= cap, f"env={env}, lev={lev}, cap={cap}, result={result}"

    def test_result_never_exceeds_original(self) -> None:
        """El ajuste solo puede reducir, nunca aumentar el leverage original."""
        cfg = _cfg()
        for original in (1, 3, 5, 7, 10):
            for env in (Environment.PAPER, Environment.TESTNET, Environment.LIVE):
                result = compute_adjusted_leverage(original, cfg, env)
                assert result <= original
