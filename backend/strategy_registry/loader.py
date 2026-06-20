"""Loader de boot: construye el StrategySetupRegistry desde config.yaml (F12)."""

from __future__ import annotations

import structlog

from backend.core.config import SetupRegistryConfig
from backend.strategy_registry.registry import StrategySetupRegistry
from backend.strategy_registry.schemas import SetupRegistrationRequest

log = structlog.get_logger(__name__)


def build_registry_from_config(cfg: SetupRegistryConfig) -> StrategySetupRegistry:
    """Construye e inicializa el registro desde la sección setup_registry del config.

    Registra todos los initial_setups declarados en config.yaml.
    Lanza ValueError si algún nombre tiene formato inválido.
    """
    registry = StrategySetupRegistry(allow_unregistered=cfg.allow_unregistered_setups)
    for name in cfg.initial_setups:
        try:
            registry.register(SetupRegistrationRequest(name=name))
        except Exception as exc:
            raise ValueError(
                f"Error registrando setup inicial '{name}' desde config: {exc}"
            ) from exc
    log.info(
        "setup_registry_initialized",
        total=len(registry),
        allow_unregistered=cfg.allow_unregistered_setups,
        setups=cfg.initial_setups,
    )
    return registry
