"""Manejo de respuestas inválidas de GPT — registro y bloqueo.

Toda respuesta fuera del contrato esperado (no-JSON, schema fail, valores fuera de
rango o reglas de negocio violadas) debe registrarse con un UUID de auditoría y nunca
resultar en una operación abierta.

Punto de entrada: handle_invalid_response() — clasifica, logea y devuelve el record.
El caller es responsable de raise GPTResponseValidationError después de la llamada.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog

_log = structlog.get_logger(__name__)

_MAX_PREVIEW_CHARS = 500

_RANGE_KEYWORDS: frozenset[str] = frozenset({"greater than", "less than"})
_BUSINESS_RULE_KEYWORD = "value error"


class InvalidResponseType(StrEnum):
    """Categoría primaria del fallo de validación de respuesta GPT."""

    NO_JSON = "NO_JSON"
    SCHEMA_FAIL = "SCHEMA_FAIL"
    RANGE_FAIL = "RANGE_FAIL"
    BUSINESS_RULE_FAIL = "BUSINESS_RULE_FAIL"


@dataclass(frozen=True, slots=True)
class InvalidResponseRecord:
    """Registro inmutable de una respuesta GPT inválida.

    Creado por handle_invalid_response() para auditoría. record_id permite
    correlacionar este registro con logs y con la tabla de auditoría futura.
    raw_content_preview está truncado a _MAX_PREVIEW_CHARS para evitar PII
    o payloads excesivos en logs y almacenamiento.
    """

    record_id: uuid.UUID
    timestamp_utc: datetime
    response_type: InvalidResponseType
    errors: list[str]
    raw_content_preview: str
    request_id: str | None = None


def classify_errors(errors: list[str]) -> InvalidResponseType:
    """Infiere el tipo primario de fallo a partir de los errores del schema guard.

    Prioridad: NO_JSON > BUSINESS_RULE_FAIL > RANGE_FAIL > SCHEMA_FAIL.
    """
    if not errors:
        return InvalidResponseType.SCHEMA_FAIL

    for err in errors:
        err_lower = err.lower()
        if "json_parse_error" in err_lower or "expected a json object" in err_lower:
            return InvalidResponseType.NO_JSON

    for err in errors:
        if _BUSINESS_RULE_KEYWORD in err.lower():
            return InvalidResponseType.BUSINESS_RULE_FAIL

    if all(any(kw in err.lower() for kw in _RANGE_KEYWORDS) for err in errors):
        return InvalidResponseType.RANGE_FAIL

    return InvalidResponseType.SCHEMA_FAIL


def handle_invalid_response(
    raw_content: str,
    errors: list[str],
    request_id: str | None = None,
) -> InvalidResponseRecord:
    """Registra y logea una respuesta GPT inválida. No lanza excepciones.

    El caller debe raise GPTResponseValidationError después de esta llamada.
    Garantiza auditoría incluso si el logger falla (best-effort logging).
    """
    response_type = classify_errors(errors)
    preview = raw_content[:_MAX_PREVIEW_CHARS]
    record = InvalidResponseRecord(
        record_id=uuid.uuid4(),
        timestamp_utc=datetime.now(UTC),
        response_type=response_type,
        errors=errors,
        raw_content_preview=preview,
        request_id=request_id,
    )
    try:
        _log.error(
            "invalid_response.blocked",
            record_id=str(record.record_id),
            response_type=record.response_type.value,
            error_count=len(errors),
            errors=errors,
            request_id=request_id,
            raw_content_preview=preview,
        )
    except Exception:  # noqa: BLE001
        pass
    return record
