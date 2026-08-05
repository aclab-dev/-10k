"""Genera el valor de DASHBOARD_PASSWORD_HASH para la auth del dashboard (F15).

Uso:
    python scripts/hash_password.py

Pide la password dos veces por stdin sin echo y solo imprime el hash. La
password nunca se acepta por argumento ni por env var: quedaría en el history
del shell o en el entorno del proceso.
"""

from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth.hashing import hash_password  # noqa: E402

_MIN_PASSWORD_LENGTH = 12


def main() -> int:
    password = getpass.getpass("Password del dashboard: ")
    if len(password) < _MIN_PASSWORD_LENGTH:
        print(
            f"La password debe tener al menos {_MIN_PASSWORD_LENGTH} caracteres.",
            file=sys.stderr,
        )
        return 1

    if password != getpass.getpass("Repetir password: "):
        print("Las passwords no coinciden.", file=sys.stderr)
        return 1

    print("\nAgregar a .env (sin comillas):\n")
    print(f"DASHBOARD_PASSWORD_HASH={hash_password(password)}")
    print(f"DASHBOARD_SECRET_KEY={secrets.token_urlsafe(48)}")
    print("\nLa secret key de arriba es nueva: al cambiarla, las sesiones activas se invalidan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
