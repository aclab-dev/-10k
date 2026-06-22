"""F12 — Strategy / Setup Registry.

Registro de estrategias y setups con versión, parámetros y metadatos.
"""

from backend.strategy_registry.loader import build_registry_from_config
from backend.strategy_registry.registry import (
    DuplicateSetupError,
    SetupNotRegisteredError,
    StrategySetupRegistry,
)
from backend.strategy_registry.schemas import (
    RegistrySnapshot,
    SetupDefinition,
    SetupMetadata,
    SetupParameters,
    SetupRegistrationRequest,
)

__all__ = [
    "build_registry_from_config",
    "DuplicateSetupError",
    "SetupNotRegisteredError",
    "StrategySetupRegistry",
    "RegistrySnapshot",
    "SetupDefinition",
    "SetupMetadata",
    "SetupParameters",
    "SetupRegistrationRequest",
]
