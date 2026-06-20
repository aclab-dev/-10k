"""Schemas Pydantic para el Strategy/Setup Registry (F12)."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_VERSION_PATTERN = re.compile(r"^(.+)_(v\d+)$")


def _parse_setup_name(name: str) -> tuple[str, str]:
    """Extrae (slug, version) de un nombre como 'breakout_pullback_v1'."""
    m = _VERSION_PATTERN.match(name)
    if not m:
        raise ValueError(
            f"Nombre de setup inválido: '{name}'. "
            "Formato esperado: <slug>_v<N>  (ej: breakout_pullback_v1)"
        )
    return m.group(1), m.group(2)


class SetupParameters(BaseModel):
    """Parámetros operativos específicos del setup.

    Todos son opcionales; None indica "usar el valor global del config".
    """

    min_confidence_override: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Override del min_confidence global."
    )
    preferred_regimes: list[str] = Field(
        default_factory=list,
        description="Regímenes de mercado donde este setup funciona mejor.",
    )
    signal_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Pesos de señales quant específicos del setup.",
    )
    tags: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class SetupMetadata(BaseModel):
    """Metadatos descriptivos del setup."""

    description: str | None = None
    author: str | None = None
    notes: str | None = None
    preferred_symbols: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class SetupDefinition(BaseModel):
    """Definición completa de un setup registrado."""

    name: str
    slug: str
    version: str
    parameters: SetupParameters = Field(default_factory=SetupParameters)
    metadata: SetupMetadata = Field(default_factory=SetupMetadata)
    is_active: bool = True
    registered_at: datetime

    model_config = {"frozen": True}

    @field_validator("name")
    @classmethod
    def name_has_version(cls, v: str) -> str:
        _parse_setup_name(v)
        return v


class SetupRegistrationRequest(BaseModel):
    """Request para registrar un nuevo setup."""

    name: str
    parameters: SetupParameters = Field(default_factory=SetupParameters)
    metadata: SetupMetadata = Field(default_factory=SetupMetadata)

    @field_validator("name")
    @classmethod
    def name_has_version(cls, v: str) -> str:
        _parse_setup_name(v)
        return v


class RegistrySnapshot(BaseModel):
    """Snapshot del estado del registro en un momento dado."""

    total: int
    active: int
    setups: list[SetupDefinition]
    snapshot_at: datetime

    model_config = {"frozen": True}
