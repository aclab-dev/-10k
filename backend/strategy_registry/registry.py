"""StrategySetupRegistry — registro en memoria de setups (F12)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from backend.strategy_registry.schemas import (
    RegistrySnapshot,
    SetupDefinition,
    SetupRegistrationRequest,
    _parse_setup_name,
)

log = structlog.get_logger(__name__)


class SetupNotRegisteredError(Exception):
    """Setup no encontrado en el registro y allow_unregistered=False."""


class DuplicateSetupError(Exception):
    """Intento de registrar un setup con nombre ya existente."""


class StrategySetupRegistry:
    """Registro en memoria de setups de trading con versión, parámetros y metadatos.

    Thread-safety: no se garantiza; el registro se construye al boot y es de solo
    lectura en producción. Mutaciones (register/deactivate) son operaciones de
    administración que no ocurren durante el ciclo de trading.
    """

    def __init__(self, allow_unregistered: bool = False) -> None:
        self._setups: dict[str, SetupDefinition] = {}
        self._allow_unregistered = allow_unregistered

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def register(self, request: SetupRegistrationRequest) -> SetupDefinition:
        """Registra un nuevo setup. Lanza DuplicateSetupError si ya existe."""
        if request.name in self._setups:
            raise DuplicateSetupError(
                f"Setup '{request.name}' ya está registrado. "
                "Usar una versión nueva (ej: _v2) para actualizaciones."
            )
        slug, version = _parse_setup_name(request.name)
        definition = SetupDefinition(
            name=request.name,
            slug=slug,
            version=version,
            parameters=request.parameters,
            metadata=request.metadata,
            is_active=True,
            registered_at=datetime.now(UTC),
        )
        self._setups[request.name] = definition
        log.info("setup_registered", name=request.name, version=version)
        return definition

    def deactivate(self, name: str) -> None:
        """Marca un setup como inactivo sin eliminarlo del registro."""
        if name not in self._setups:
            raise SetupNotRegisteredError(f"Setup '{name}' no existe en el registro.")
        current = self._setups[name]
        # SetupDefinition es frozen; recrear con is_active=False
        self._setups[name] = current.model_copy(update={"is_active": False})
        log.info("setup_deactivated", name=name)

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def get(self, name: str) -> SetupDefinition | None:
        """Retorna la definición del setup o None si no existe."""
        return self._setups.get(name)

    def validate(self, setup_name: str) -> SetupDefinition:
        """Valida que el setup exista y esté activo.

        Si allow_unregistered=True y el setup no existe, retorna una definición
        mínima con slug/version derivados del nombre.
        Lanza SetupNotRegisteredError si allow_unregistered=False y no existe.
        Lanza ValueError si allow_unregistered=True pero el nombre tiene formato inválido.
        """
        definition = self._setups.get(setup_name)
        if definition is not None:
            return definition
        if self._allow_unregistered:
            log.warning("setup_not_registered_allowed", name=setup_name)
            slug, version = _parse_setup_name(setup_name)
            return SetupDefinition(
                name=setup_name,
                slug=slug,
                version=version,
                is_active=True,
                registered_at=datetime.now(UTC),
            )
        raise SetupNotRegisteredError(
            f"Setup '{setup_name}' no está registrado. "
            "Registrarlo antes de usarlo o habilitar allow_unregistered_setups en config."
        )

    def list_active(self) -> list[SetupDefinition]:
        """Retorna todos los setups activos ordenados por nombre."""
        return sorted(
            (s for s in self._setups.values() if s.is_active),
            key=lambda s: s.name,
        )

    def snapshot(self) -> RegistrySnapshot:
        """Retorna un snapshot inmutable del estado actual del registro."""
        all_setups = list(self._setups.values())
        active = [s for s in all_setups if s.is_active]
        return RegistrySnapshot(
            total=len(all_setups),
            active=len(active),
            setups=sorted(all_setups, key=lambda s: s.name),
            snapshot_at=datetime.now(UTC),
        )

    def __len__(self) -> int:
        return len(self._setups)

    def __contains__(self, name: str) -> bool:
        return name in self._setups
