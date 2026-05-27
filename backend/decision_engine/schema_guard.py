"""Schema guard — valida el JSON raw del GPT contra ModelDecision.

Punto de entrada único para parsear la respuesta del modelo. Devuelve
siempre un resultado tipado en lugar de propagar excepciones, para que
los callers puedan decidir cómo manejar outputs inválidos sin try/except.

Cualquier respuesta fuera de schema bloquea la ejecución y registra el
motivo en structlog para auditoría.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from backend.decision_engine.schemas import ModelDecision

_log = structlog.get_logger(__name__)


class SchemaGuardResult:
    """Resultado del guard: éxito o fallo con lista de errores."""

    __slots__ = ("ok", "decision", "errors")

    def __init__(
        self,
        ok: bool,
        decision: ModelDecision | None,
        errors: list[str],
    ) -> None:
        self.ok = ok
        self.decision = decision
        self.errors = errors

    def __bool__(self) -> bool:
        return self.ok


def validate_gpt_response(raw: dict[str, Any]) -> SchemaGuardResult:
    """Parsea y valida el dict raw del GPT.

    Retorna SchemaGuardResult con ok=True y la decisión validada,
    o ok=False con la lista de errores de validación. En caso de fallo,
    loguea el motivo para auditoría antes de retornar.
    """
    try:
        decision = ModelDecision.model_validate(raw)
        _log.info(
            "schema_guard.accepted",
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            decision=decision.decision,
            environment=decision.environment,
        )
        return SchemaGuardResult(ok=True, decision=decision, errors=[])
    except ValidationError as exc:
        errors = [f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()]
        # Log best-effort: si el propio logger falla no debe romper el contrato
        # de "siempre retorna SchemaGuardResult". El caller no debe recibir una
        # excepción inesperada salida del guard.
        try:
            _log.warning(
                "schema_guard.blocked",
                reason="schema_validation_failed",
                error_count=len(errors),
                errors=errors,
                raw_keys=list(raw.keys()) if isinstance(raw, dict) else None,
                raw_type=type(raw).__name__,
            )
        except Exception:  # noqa: BLE001
            pass
        return SchemaGuardResult(ok=False, decision=None, errors=errors)
    except Exception as exc:  # noqa: BLE001
        try:
            _log.error(
                "schema_guard.unexpected_error",
                reason="unexpected_exception",
                error=str(exc),
                exc_info=True,
            )
        except Exception:  # noqa: BLE001
            pass
        return SchemaGuardResult(ok=False, decision=None, errors=[str(exc)])
