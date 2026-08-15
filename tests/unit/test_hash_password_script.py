"""Tests de scripts/hash_password.py — escapado para Docker Compose (F15)."""

from __future__ import annotations

import pytest

from scripts.hash_password import escape_dollar_for_dotenv


def test_escapes_every_dollar_in_a_scrypt_hash() -> None:
    hashed = "scrypt$32768$8$1$c2FsdA$aGFzaA"
    assert escape_dollar_for_dotenv(hashed) == "scrypt$$32768$$8$$1$$c2FsdA$$aGFzaA"


def test_leaves_a_value_without_dollar_untouched() -> None:
    assert escape_dollar_for_dotenv("no-dollar-here") == "no-dollar-here"


def test_empty_string_stays_empty() -> None:
    assert escape_dollar_for_dotenv("") == ""


@pytest.mark.parametrize("dollar_count", [1, 2, 5, 10])
def test_escaped_value_has_exactly_double_the_dollars(dollar_count: int) -> None:
    value = "$" * dollar_count
    escaped = escape_dollar_for_dotenv(value)
    assert escaped == "$$" * dollar_count
    assert escaped.count("$") == dollar_count * 2
