"""Tests de backend/auth/hashing.py — derivación scrypt y verificación."""

from __future__ import annotations

import pytest

from backend.auth.hashing import (
    hash_password,
    validate_password_hash,
    verify_password,
)

# Parámetros de costo mínimos: los tests verifican la lógica del formato, no la
# resistencia de scrypt. Con los defaults (n=2**15) cada derivación cuesta
# ~100 ms y este módulo tardaría segundos.
_FAST = {"n": 2**4, "r": 1, "p": 1}


def fast_hash(password: str) -> str:
    return hash_password(password, **_FAST)


def test_hash_verifies_against_original_password() -> None:
    encoded = fast_hash("una-password-cualquiera")
    assert verify_password("una-password-cualquiera", encoded) is True


def test_hash_rejects_wrong_password() -> None:
    encoded = fast_hash("la-correcta")
    assert verify_password("la-incorrecta", encoded) is False


def test_hash_rejects_password_with_trailing_whitespace() -> None:
    encoded = fast_hash("password")
    assert verify_password("password ", encoded) is False


def test_same_password_produces_different_hashes() -> None:
    """Salt aleatorio: dos hashes de la misma password no pueden coincidir."""
    first = fast_hash("misma-password")
    second = fast_hash("misma-password")
    assert first != second
    assert verify_password("misma-password", first) is True
    assert verify_password("misma-password", second) is True


def test_hash_encodes_its_own_cost_parameters() -> None:
    encoded = hash_password("x", n=2**5, r=2, p=1)
    algorithm, n, r, p, _salt, _digest = encoded.split("$")
    assert (algorithm, n, r, p) == ("scrypt", "32", "2", "1")
    # Verifica con los parámetros del hash, no con los defaults del módulo.
    assert verify_password("x", encoded) is True


def test_hash_supports_non_ascii_password() -> None:
    encoded = fast_hash("contraseña-ñandú-🔐")
    assert verify_password("contraseña-ñandú-🔐", encoded) is True


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "no-tiene-separadores",
        "scrypt$16$1$1$solo-cinco-campos",
        "scrypt$16$1$1$c2FsdA$aGFzaA$de-mas",
        "bcrypt$16$1$1$c2FsdA$aGFzaA",  # algoritmo desconocido
        "scrypt$no-un-numero$1$1$c2FsdA$aGFzaA",
        "scrypt$15$1$1$c2FsdA$aGFzaA",  # n no es potencia de 2
        "scrypt$1$1$1$c2FsdA$aGFzaA",  # n debe ser > 1
        "scrypt$16$0$1$c2FsdA$aGFzaA",  # r < 1
        "scrypt$16$1$0$c2FsdA$aGFzaA",  # p < 1
        "scrypt$16$1$1$$aGFzaA",  # salt vacío
        "scrypt$16$1$1$c2FsdA$",  # digest vacío
        "scrypt$16$1$1$!!!no-base64!!!$aGFzaA",
    ],
)
def test_malformed_hash_returns_false_instead_of_raising(malformed: str) -> None:
    """Un hash corrupto se trata como password incorrecta, no como excepción."""
    assert verify_password("cualquiera", malformed) is False
    assert validate_password_hash(malformed) is False


def test_validate_accepts_a_freshly_generated_hash() -> None:
    assert validate_password_hash(fast_hash("password")) is True


def test_validate_does_not_check_the_password() -> None:
    """validate_password_hash mira estructura, no deriva ni compara."""
    encoded = fast_hash("password")
    assert validate_password_hash(encoded) is True
    assert verify_password("otra", encoded) is False
