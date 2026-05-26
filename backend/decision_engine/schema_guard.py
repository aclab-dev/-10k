"""Schema guard — valida el JSON raw del GPT contra GPTDecisionResponse.

Punto de entrada único para parsear la respuesta del modelo. Devuelve
siempre un resultado tipado en lugar de propagar excepciones, para que
los callers puedan decidir cómo manejar outputs inválidos sin try/except.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from backend.decision_engine.schemas import GPTDecisionResponse


class SchemaGuardResult:
    """Resultado del guard: éxito o fallo con lista de errores."""

    __slots__ = ("ok", "decision", "errors")

    def __init__(
        self,
        ok: bool,
        decision: GPTDecisionResponse | None,
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
    o ok=False con la lista de errores de validación.
    """
    try:
        decision = GPTDecisionResponse.model_validate(raw)
        return SchemaGuardResult(ok=True, decision=decision, errors=[])
    except ValidationError as exc:
        errors = [f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return SchemaGuardResult(ok=False, decision=None, errors=errors)
    except Exception as exc:  # noqa: BLE001
        return SchemaGuardResult(ok=False, decision=None, errors=[str(exc)])
