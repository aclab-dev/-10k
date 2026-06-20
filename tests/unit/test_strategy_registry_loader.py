"""Tests unitarios para el loader del Strategy/Setup Registry (F12)."""

from __future__ import annotations

import pytest

from backend.core.config import SetupRegistryConfig
from backend.strategy_registry.loader import build_registry_from_config
from backend.strategy_registry.registry import StrategySetupRegistry


def _cfg(
    initial_setups: list[str],
    allow_unregistered: bool = False,
    required: bool = True,
) -> SetupRegistryConfig:
    return SetupRegistryConfig(
        required=required,
        allow_unregistered_setups=allow_unregistered,
        initial_setups=initial_setups,
    )


class TestBuildRegistryFromConfig:
    def test_registers_all_initial_setups(self) -> None:
        cfg = _cfg(["breakout_pullback_v1", "trend_continuation_v1"])
        registry = build_registry_from_config(cfg)
        assert len(registry) == 2
        assert "breakout_pullback_v1" in registry
        assert "trend_continuation_v1" in registry

    def test_empty_initial_setups(self) -> None:
        registry = build_registry_from_config(_cfg([]))
        assert len(registry) == 0

    def test_all_five_default_setups(self) -> None:
        initial = [
            "breakout_pullback_v1",
            "liquidity_sweep_reversal_v1",
            "trend_continuation_v1",
            "mean_reversion_extreme_v1",
            "funding_oi_divergence_v1",
        ]
        registry = build_registry_from_config(_cfg(initial))
        assert len(registry) == 5
        for name in initial:
            assert name in registry

    def test_all_initial_setups_are_active(self) -> None:
        cfg = _cfg(["breakout_pullback_v1", "mean_reversion_extreme_v1"])
        registry = build_registry_from_config(cfg)
        assert len(registry.list_active()) == 2

    def test_invalid_setup_name_raises(self) -> None:
        with pytest.raises(ValueError, match="breakout_pullback"):
            build_registry_from_config(_cfg(["breakout_pullback"]))  # sin _vN

    def test_allow_unregistered_propagated(self) -> None:
        cfg = _cfg([], allow_unregistered=True)
        registry = build_registry_from_config(cfg)
        # should not raise even for unknown setups
        definition = registry.validate("any_custom_setup_v1")
        assert definition.name == "any_custom_setup_v1"

    def test_returns_strategy_setup_registry_instance(self) -> None:
        registry = build_registry_from_config(_cfg(["breakout_pullback_v1"]))
        assert isinstance(registry, StrategySetupRegistry)
