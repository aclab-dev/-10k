"""Verifica que los enlaces a línea de los docs sigan apuntando a lo que anuncian.

`docs/live_checklist.md` es el gate de la regla 34: su valor está en que cada
fila cite evidencia verificable. Esas citas son enlaces a un archivo y una
línea, y cualquier commit que agregue un import o una constante los corre en
silencio — el documento sigue renderizando, pero apunta a una línea en blanco
o a otra función. Pasó cuatro veces en una sola card.

Regla que aplica este script: si la etiqueta del enlace es un símbolo Python
(`` [`check_funding_gate`](../backend/risk_engine/checks.py#L217) ``), ese
símbolo tiene que aparecer en la línea destino o en la definición que la
contiene. Un enlace que apunta a propósito al *cuerpo* de algo — una línea
concreta dentro de una función — se etiqueta con su ubicación
(`` [`engine.py:146`](...#L146) ``) y el script no le exige símbolo, sólo que
la línea no esté vacía.

Lo que NO detecta, a propósito: que un ancla se corra unas líneas pero siga
dentro del mismo cuerpo. Exigir la línea exacta daría falsos positivos en cada
enlace que apunta al medio de una función, y un linter ruidoso se termina
ignorando. Atrapa lo que rompe el documento de verdad — el ancla que aterriza
en otra función, en una línea en blanco o fuera de rango — que es justo lo que
produce agregar un import o una constante más arriba.

Uso: `python scripts/check_doc_anchors.py [docs/live_checklist.md ...]`
Sin argumentos revisa todos los .md de docs/. Sale con 1 si algo no verifica.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: [etiqueta](../ruta/al/archivo.py#L123)
_LINK = re.compile(r"\[([^\]]+)\]\((\.\./[A-Za-z0-9_./-]+\.py)#L(\d+)\)")
#: Etiquetas que son una ubicación, no un símbolo: `engine.py:146`, `execution/engine.py:362`.
_LOCATION_LABEL = re.compile(r"^`?[A-Za-z0-9_/]+\.py:\d+`?$")
#: Un símbolo Python, eventualmente cualificado: `PaperAdapter._cross_spread`.
_SYMBOL_LABEL = re.compile(r"^`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`$")
_DEF = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")


def _enclosing_definitions(lines: list[str], index: int) -> list[str]:
    """Nombres de las definiciones que contienen a `lines[index]`, de dentro hacia fuera."""
    names: list[str] = []
    indent = len(lines[index]) - len(lines[index].lstrip())
    for i in range(index, -1, -1):
        m = _DEF.match(lines[i])
        if m is None:
            continue
        def_indent = len(m.group(1))
        if i == index or def_indent < indent:
            names.append(m.group(2))
            indent = def_indent
            if def_indent == 0:
                break
    return names


def check_file(doc: Path) -> list[str]:
    problems: list[str] = []
    text = doc.read_text(encoding="utf-8")
    for label, rel_path, raw_line in _LINK.findall(text):
        target = (doc.parent / rel_path).resolve()
        line_no = int(raw_line)
        if not target.exists():
            problems.append(f"{doc.name}: [{label}] → {rel_path} no existe")
            continue
        lines = target.read_text(encoding="utf-8").splitlines()
        if line_no > len(lines):
            problems.append(
                f"{doc.name}: [{label}] → {rel_path}#L{line_no} fuera de rango "
                f"({len(lines)} líneas)"
            )
            continue

        content = lines[line_no - 1]
        if not content.strip():
            problems.append(f"{doc.name}: [{label}] → {rel_path}#L{line_no} es una línea vacía")
            continue

        if _LOCATION_LABEL.match(label):
            continue  # etiqueta-ubicación: alcanza con que la línea exista
        m = _SYMBOL_LABEL.match(label)
        if m is None:
            continue  # prosa ("docstring del módulo"): no hay símbolo que exigir

        symbol = m.group(1).split(".")[-1]
        if symbol in content or symbol in _enclosing_definitions(lines, line_no - 1):
            continue
        problems.append(
            f"{doc.name}: [{label}] → {rel_path}#L{line_no} no menciona '{symbol}': "
            f"{content.strip()[:60]}"
        )
    return problems


def main(argv: list[str]) -> int:
    docs = [Path(a) for a in argv[1:]] or sorted((REPO_ROOT / "docs").glob("*.md"))
    problems: list[str] = []
    for doc in docs:
        problems.extend(check_file(doc))

    if problems:
        print(f"Anclas rotas ({len(problems)}):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nCorregilas apuntando a la línea correcta, o usá una etiqueta-ubicación "
            "(`archivo.py:NN`) si el enlace apunta a propósito al cuerpo de algo.",
            file=sys.stderr,
        )
        return 1

    checked = sum(len(_LINK.findall(d.read_text(encoding="utf-8"))) for d in docs)
    print(f"{checked} anclas verificadas en {len(docs)} documento(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
