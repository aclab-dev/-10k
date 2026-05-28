"""Tests unitarios para invalid_response_handler — clasificación, registro y bloqueo."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC

import pytest
from structlog.testing import capture_logs

from backend.decision_engine.invalid_response_handler import (
    InvalidResponseRecord,
    InvalidResponseType,
    classify_errors,
    handle_invalid_response,
)

# ---------------------------------------------------------------------------
# Tests: classify_errors
# ---------------------------------------------------------------------------


def test_classify_no_json_parse_error() -> None:
    """Error de json.loads → NO_JSON."""
    errors = ["json_parse_error: Expecting value: line 1 column 1 (char 0)"]
    assert classify_errors(errors) == InvalidResponseType.NO_JSON


def test_classify_no_json_not_dict() -> None:
    """Respuesta JSON que no es objeto (lista, primitivo) → NO_JSON."""
    errors = ["Expected a JSON object, got list"]
    assert classify_errors(errors) == InvalidResponseType.NO_JSON


def test_classify_business_rule_fail() -> None:
    """Error de validador de regla de negocio (Value error) → BUSINESS_RULE_FAIL."""
    errors = ["execute: Value error, execute=True requiere stop_loss > 0"]
    assert classify_errors(errors) == InvalidResponseType.BUSINESS_RULE_FAIL


def test_classify_range_fail_all_range_errors() -> None:
    """Todos los errores son de rango → RANGE_FAIL."""
    errors = [
        "leverage: Input should be less than or equal to 10",
        "margin_usdt: Input should be greater than or equal to 0",
    ]
    assert classify_errors(errors) == InvalidResponseType.RANGE_FAIL


def test_classify_range_fail_single_error() -> None:
    """Un único error de rango → RANGE_FAIL."""
    errors = ["confidence: Input should be less than or equal to 1"]
    assert classify_errors(errors) == InvalidResponseType.RANGE_FAIL


def test_classify_schema_fail_missing_field() -> None:
    """Campo requerido faltante → SCHEMA_FAIL."""
    errors = ["symbol: Field required"]
    assert classify_errors(errors) == InvalidResponseType.SCHEMA_FAIL


def test_classify_schema_fail_type_error() -> None:
    """Error de tipo → SCHEMA_FAIL."""
    errors = ["leverage: Input should be a valid integer"]
    assert classify_errors(errors) == InvalidResponseType.SCHEMA_FAIL


def test_classify_empty_errors_returns_schema_fail() -> None:
    """Lista vacía de errores → SCHEMA_FAIL por defecto."""
    assert classify_errors([]) == InvalidResponseType.SCHEMA_FAIL


def test_classify_mixed_range_and_schema_errors() -> None:
    """Mezcla de rango y tipo → SCHEMA_FAIL (no todos son rango)."""
    errors = [
        "leverage: Input should be less than or equal to 10",
        "symbol: Field required",
    ]
    assert classify_errors(errors) == InvalidResponseType.SCHEMA_FAIL


def test_classify_no_json_takes_priority_over_business_rule() -> None:
    """NO_JSON tiene prioridad más alta que BUSINESS_RULE_FAIL."""
    errors = [
        "json_parse_error: invalid syntax",
        "execute: Value error, regla de negocio",
    ]
    assert classify_errors(errors) == InvalidResponseType.NO_JSON


# ---------------------------------------------------------------------------
# Tests: handle_invalid_response — structure del record
# ---------------------------------------------------------------------------


def test_handle_returns_invalid_response_record() -> None:
    """handle_invalid_response devuelve un InvalidResponseRecord."""
    with capture_logs():
        record = handle_invalid_response("not json {{{", ["json_parse_error: bad"])

    assert isinstance(record, InvalidResponseRecord)


def test_handle_record_has_valid_uuid() -> None:
    """record_id es un UUID válido."""
    with capture_logs():
        record = handle_invalid_response("content", ["json_parse_error: x"])

    assert isinstance(record.record_id, uuid.UUID)


def test_handle_record_has_utc_timestamp() -> None:
    """timestamp_utc tiene tzinfo UTC."""
    with capture_logs():
        record = handle_invalid_response("content", ["json_parse_error: x"])

    assert record.timestamp_utc.tzinfo is not None
    assert record.timestamp_utc.tzinfo == UTC


def test_handle_record_response_type_set() -> None:
    """response_type refleja la clasificación de los errores."""
    with capture_logs():
        record = handle_invalid_response("x", ["Expected a JSON object, got list"])

    assert record.response_type == InvalidResponseType.NO_JSON


def test_handle_record_errors_preserved() -> None:
    """errors del record coinciden con los pasados (como tuple inmutable)."""
    errors = ["symbol: Field required", "decision: Field required"]
    with capture_logs():
        record = handle_invalid_response("{}", errors)

    assert record.errors == tuple(errors)


def test_handle_record_request_id_none_by_default() -> None:
    """request_id=None cuando no se pasa."""
    with capture_logs():
        record = handle_invalid_response("x", ["json_parse_error: x"])

    assert record.request_id is None


def test_handle_record_request_id_stored() -> None:
    """request_id se almacena en el record."""
    with capture_logs():
        record = handle_invalid_response("x", ["json_parse_error: x"], request_id="v1.0")

    assert record.request_id == "v1.0"


# ---------------------------------------------------------------------------
# Tests: truncación de raw_content_preview
# ---------------------------------------------------------------------------


def test_handle_raw_content_short_not_truncated() -> None:
    """Contenido de menos de 500 chars no se trunca."""
    raw = "short content"
    with capture_logs():
        record = handle_invalid_response(raw, ["json_parse_error: x"])

    assert record.raw_content_preview == raw


def test_handle_raw_content_exactly_500_chars_not_truncated() -> None:
    """Contenido de exactamente 500 chars no se trunca."""
    raw = "x" * 500
    with capture_logs():
        record = handle_invalid_response(raw, ["json_parse_error: x"])

    assert len(record.raw_content_preview) == 500


def test_handle_raw_content_truncated_at_500() -> None:
    """Contenido mayor a 500 chars se trunca a exactamente 500."""
    raw = "a" * 1000
    with capture_logs():
        record = handle_invalid_response(raw, ["json_parse_error: x"])

    assert len(record.raw_content_preview) == 500
    assert record.raw_content_preview == "a" * 500


def test_handle_raw_content_large_payload_truncated() -> None:
    """Payload grande se trunca — nunca supera 500 chars en logs."""
    raw = '{"key": "' + "x" * 10_000 + '"}'
    with capture_logs():
        record = handle_invalid_response(raw, ["symbol: Field required"])

    assert len(record.raw_content_preview) <= 500


# ---------------------------------------------------------------------------
# Tests: inmutabilidad del record
# ---------------------------------------------------------------------------


def test_record_is_frozen() -> None:
    """InvalidResponseRecord es inmutable (frozen dataclass)."""
    with capture_logs():
        record = handle_invalid_response("x", ["json_parse_error: x"])

    with pytest.raises((FrozenInstanceError, AttributeError)):
        record.response_type = InvalidResponseType.SCHEMA_FAIL  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: logging estructurado
# ---------------------------------------------------------------------------


def test_handle_logs_blocked_event() -> None:
    """handle_invalid_response loguea el evento 'invalid_response.blocked'."""
    with capture_logs() as cap_logs:
        handle_invalid_response("bad content", ["json_parse_error: unexpected"])

    assert len(cap_logs) == 1
    assert cap_logs[0]["event"] == "invalid_response.blocked"


def test_handle_logs_response_type() -> None:
    """El log incluye response_type como string."""
    with capture_logs() as cap_logs:
        handle_invalid_response("{}", ["symbol: Field required"])

    assert cap_logs[0]["response_type"] == "SCHEMA_FAIL"


def test_handle_logs_error_count() -> None:
    """El log incluye error_count correcto."""
    errors = ["a: Field required", "b: Field required", "c: Field required"]
    with capture_logs() as cap_logs:
        handle_invalid_response("{}", errors)

    assert cap_logs[0]["error_count"] == 3


def test_handle_logs_record_id_as_string() -> None:
    """El log incluye record_id como string (UUID serializable)."""
    with capture_logs() as cap_logs:
        handle_invalid_response("x", ["json_parse_error: x"])

    assert isinstance(cap_logs[0]["record_id"], str)
    uuid.UUID(cap_logs[0]["record_id"])  # debe ser UUID válido


def test_handle_logs_request_id_when_provided() -> None:
    """El log incluye request_id si se pasó."""
    with capture_logs() as cap_logs:
        handle_invalid_response("x", ["json_parse_error: x"], request_id="prompt-v1.0")

    assert cap_logs[0]["request_id"] == "prompt-v1.0"


def test_handle_logs_raw_preview_truncated() -> None:
    """El log incluye raw_content_preview truncado, no el payload completo."""
    raw = "z" * 5000
    with capture_logs() as cap_logs:
        handle_invalid_response(raw, ["json_parse_error: x"])

    assert len(cap_logs[0]["raw_content_preview"]) <= 500
