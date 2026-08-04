"""Hashing de passwords con scrypt (stdlib) — sin dependencias externas.

Formato encoded: `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`.

Los parámetros van embebidos en el string para que un hash generado hoy siga
verificando aunque mañana subamos el costo: `verify_password` usa los del hash,
no los defaults del módulo.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from functools import lru_cache

# Parámetros de costo. n debe ser potencia de 2. n=2**15, r=8, p=1 es el perfil
# "interactive" de referencia de scrypt: ~64 MB de RAM por verificación, lo que
# hace inviable el brute-force offline si el hash se filtra.
_DEFAULT_N = 2**15
_DEFAULT_R = 8
_DEFAULT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_ALGORITHM = "scrypt"
_FIELD_COUNT = 6


def _maxmem(n: int, r: int) -> int:
    """scrypt exige maxmem >= 128 * n * r; el default de OpenSSL (32 MB) no alcanza
    para n=2**15, r=8 (~64 MB). Lo calculamos con margen a partir de los parámetros
    reales del hash, no de los defaults del módulo."""
    return 128 * n * r * 2


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(
    password: str,
    *,
    n: int = _DEFAULT_N,
    r: int = _DEFAULT_R,
    p: int = _DEFAULT_P,
) -> str:
    """Deriva un hash scrypt con salt aleatorio y lo devuelve en formato encoded.

    El resultado es seguro de persistir en `DASHBOARD_PASSWORD_HASH`: no permite
    recuperar la password original ni verificarla sin pagar el costo de scrypt.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_BYTES,
        maxmem=_maxmem(n, r),
    )
    return f"{_ALGORITHM}${n}${r}${p}${_b64encode(salt)}${_b64encode(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Verifica una password contra un hash encoded, en tiempo constante.

    Devuelve `False` ante un hash malformado en vez de propagar: el llamador
    (login) trata "hash corrupto" igual que "password incorrecta" para no
    filtrar el estado de la configuración por la respuesta HTTP. El caso se
    detecta al boot vía `validate_password_hash`, no acá.
    """
    parts = encoded.split("$")
    if len(parts) != _FIELD_COUNT or parts[0] != _ALGORITHM:
        return False

    _, n_raw, r_raw, p_raw, salt_b64, hash_b64 = parts
    try:
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
    except (ValueError, binascii.Error):
        return False

    if n <= 1 or n & (n - 1) or r < 1 or p < 1 or not salt or not expected:
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_maxmem(n, r),
        )
    except (ValueError, MemoryError):
        # Parámetros sintácticamente válidos pero fuera de rango para scrypt
        # (n absurdamente grande, por ejemplo). Mismo trato que hash corrupto.
        return False

    return hmac.compare_digest(derived, expected)


def validate_password_hash(encoded: str) -> bool:
    """Chequea que un hash encoded sea estructuralmente válido, sin derivar nada.

    Se usa al boot para fallar rápido si `DASHBOARD_PASSWORD_HASH` está mal
    escrito, en vez de descubrirlo en el primer login fallido.
    """
    parts = encoded.split("$")
    if len(parts) != _FIELD_COUNT or parts[0] != _ALGORITHM:
        return False

    _, n_raw, r_raw, p_raw, salt_b64, hash_b64 = parts
    try:
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _b64decode(salt_b64)
        digest = _b64decode(hash_b64)
    except (ValueError, binascii.Error):
        return False

    return n > 1 and not n & (n - 1) and r >= 1 and p >= 1 and bool(salt) and bool(digest)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Hash de una password aleatoria, para el "dummy verify" del login.

    Verificar contra este hash cuando el usuario no existe cuesta lo mismo que
    una verificación real, así el atacante no distingue usuario válido de
    inválido por el tiempo de respuesta. Cacheado: derivarlo cuesta ~64 MB de
    RAM y no tiene sentido pagarlo en cada request (ni al importar el módulo).
    """
    return hash_password(secrets.token_urlsafe(32))
