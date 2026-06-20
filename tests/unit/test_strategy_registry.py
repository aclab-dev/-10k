"""Tests unitarios para el Strategy/Setup Registry (F12)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.strategy_registry.registry import (
    DuplicateSetupError,
    SetupNotRegisteredError,
    StrategySetupRegistry,
)
from backend.strategy_registry.schemas import (
    SetupMetadata,
    SetupParameters,
    SetupRegistrationRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(name: str, **kwargs: object) -> SetupRegistrationRequest:
    return SetupRegistrationRequest(name=name, **kwargs)


# ---------------------------------------------------------------------------
# SetupRegistrationRequest — validación de nombre
# ---------------------------------------------------------------------------


class TestSetupRegistrationRequestValidation:
    def test_valid_name_accepted(self) -> None:
        req = _req("breakout_pullback_v1")
        assert req.name == "breakout_pullback_v1"

    def test_invalid_name_no_version_raises(self) -> None:
        with pytest.raises(ValueError, match="Formato esperado"):
            _req("breakout_pullback")

    def test_invalid_name_wrong_suffix_raises(self) -> None:
        with pytest.raises(ValueError, match="Formato esperado"):
            _req("breakout_pullback_1")

    def test_name_with_multiple_underscores(self) -> None:
        req = _req("liquidity_sweep_reversal_v1")
        assert req.name == "liquidity_sweep_reversal_v1"


# ---------------------------------------------------------------------------
# StrategySetupRegistry — registro y duplicados
# ---------------------------------------------------------------------------


class TestRegistryRegister:
    def test_register_returns_definition(self) -> None:
        registry = StrategySetupRegistry()
        definition = registry.register(_req("breakout_pullback_v1"))
        assert definition.name == "breakout_pullback_v1"
        assert definition.slug == "breakout_pullback"
        assert definition.version == "v1"
        assert definition.is_active is True

    def test_register_parses_slug_and_version(self) -> None:
        registry = StrategySetupRegistry()
        definition = registry.register(_req("liquidity_sweep_reversal_v2"))
        assert definition.slug == "liquidity_sweep_reversal"
        assert definition.version == "v2"

    def test_register_duplicate_raises(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        with pytest.raises(DuplicateSetupError):
            registry.register(_req("breakout_pullback_v1"))

    def test_register_different_versions_allowed(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        registry.register(_req("breakout_pullback_v2"))
        assert len(registry) == 2

    def test_register_with_parameters(self) -> None:
        params = SetupParameters(
            min_confidence_override=0.80,
            preferred_regimes=["TRENDING_UP"],
            signal_weights={"momentum": 1.2},
        )
        registry = StrategySetupRegistry()
        definition = registry.register(_req("trend_continuation_v1", parameters=params))
        assert definition.parameters.min_confidence_override == 0.80
        assert definition.parameters.preferred_regimes == ["TRENDING_UP"]
        assert definition.parameters.signal_weights == {"momentum": 1.2}

    def test_register_with_metadata(self) -> None:
        meta = SetupMetadata(description="Breakout en pullback", author="quant_team")
        registry = StrategySetupRegistry()
        definition = registry.register(_req("breakout_pullback_v1", metadata=meta))
        assert definition.metadata.description == "Breakout en pullback"
        assert definition.metadata.author == "quant_team"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestRegistryValidate:
    def test_validate_registered_setup_returns_definition(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        definition = registry.validate("breakout_pullback_v1")
        assert definition.name == "breakout_pullback_v1"

    def test_validate_unregistered_raises_when_strict(self) -> None:
        registry = StrategySetupRegistry(allow_unregistered=False)
        with pytest.raises(SetupNotRegisteredError, match="breakout_pullback_v1"):
            registry.validate("breakout_pullback_v1")

    def test_validate_unregistered_allowed_returns_minimal_definition(self) -> None:
        registry = StrategySetupRegistry(allow_unregistered=True)
        definition = registry.validate("custom_setup_v3")
        assert definition.name == "custom_setup_v3"
        assert definition.slug == "custom_setup"
        assert definition.version == "v3"
        assert definition.is_active is True

    def test_validate_unregistered_allowed_invalid_name_raises(self) -> None:
        registry = StrategySetupRegistry(allow_unregistered=True)
        with pytest.raises(ValueError):
            registry.validate("no_version_here")


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


class TestRegistryGet:
    def test_get_existing_returns_definition(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        assert registry.get("breakout_pullback_v1") is not None

    def test_get_missing_returns_none(self) -> None:
        registry = StrategySetupRegistry()
        assert registry.get("nonexistent_v1") is None


# ---------------------------------------------------------------------------
# deactivate
# ---------------------------------------------------------------------------


class TestRegistryDeactivate:
    def test_deactivate_marks_inactive(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        registry.deactivate("breakout_pullback_v1")
        assert registry.get("breakout_pullback_v1").is_active is False  # type: ignore[union-attr]

    def test_deactivate_nonexistent_raises(self) -> None:
        registry = StrategySetupRegistry()
        with pytest.raises(SetupNotRegisteredError):
            registry.deactivate("nonexistent_v1")

    def test_deactivated_setup_excluded_from_list_active(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        registry.register(_req("trend_continuation_v1"))
        registry.deactivate("breakout_pullback_v1")
        active = registry.list_active()
        assert len(active) == 1
        assert active[0].name == "trend_continuation_v1"


# ---------------------------------------------------------------------------
# list_active / snapshot
# ---------------------------------------------------------------------------


class TestRegistrySnapshot:
    def test_list_active_sorted_by_name(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("trend_continuation_v1"))
        registry.register(_req("breakout_pullback_v1"))
        active = registry.list_active()
        assert [s.name for s in active] == ["breakout_pullback_v1", "trend_continuation_v1"]

    def test_snapshot_counts(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        registry.register(_req("trend_continuation_v1"))
        registry.deactivate("trend_continuation_v1")
        snap = registry.snapshot()
        assert snap.total == 2
        assert snap.active == 1

    def test_snapshot_is_frozen(self) -> None:
        registry = StrategySetupRegistry()
        snap = registry.snapshot()
        with pytest.raises(ValidationError):
            snap.total = 99  # type: ignore[misc]

    def test_contains(self) -> None:
        registry = StrategySetupRegistry()
        registry.register(_req("breakout_pullback_v1"))
        assert "breakout_pullback_v1" in registry
        assert "nonexistent_v1" not in registry
