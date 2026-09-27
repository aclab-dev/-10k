"""Verifica que las referencias a código de los docs sigan apuntando a algo que existe.

`docs/live_checklist.md` es el gate de la regla 34: su valor está en que cada
fila cite evidencia verificable y auditable (spec §3.6). Las citas por número
de línea (`archivo.py#L123`, `archivo.py:123`) se corren en silencio con
cualquier import o constante nueva — pasó en casi cada card de F17 — y un
linter que sólo mira la línea destino no distingue "se corrió dentro del mismo
cuerpo" de "ahora apunta a otra cosa".

Por eso la evidencia se cita por símbolo, que no se mueve cuando el archivo
cambia, y este script la verifica con `ast`:

- Enlace a código: `` [`check_funding_gate`](../backend/risk_engine/checks.py) ``.
  Cada símbolo entre backticks de la etiqueta tiene que estar definido en el
  archivo destino (función, clase, método, campo de clase o constante de
  módulo). Un nombre cualificado (`RiskConfig.no_cross`) se resuelve exacto;
  uno suelto, contra el nombre de módulo o el último componente.
- Test: `` `tests/unit/test_config.py::test_rejects_cross` `` (node id de
  pytest). El archivo y cada nombre de la ruta tienen que existir.
- Prohibido: `#L<n>` en un enlace y `` `archivo.py:<n>` `` en el texto.

Uso: `python scripts/check_doc_anchors.py [docs/live_checklist.md ...]`
Sin argumentos revisa todos los .md de docs/. Sale con 1 si algo no verifica.
"""

from __future__ import annotations

import ast
import re
import sys
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: [etiqueta](../ruta/al/archivo.py) con `#L123` opcional (que se rechaza).
_LINK = re.compile(r"\[([^\]]+)\]\((\.\./[A-Za-z0-9_./-]+\.py)(#L\d+)?\)")
#: Un símbolo Python entre backticks, eventualmente cualificado: `PaperAdapter._cross_spread`.
_SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")
#: Cita por línea en el texto: `engine.py:146`, `tests/unit/test_x.py:23-172`.
_LINE_REF = re.compile(r"`[A-Za-z0-9_./-]+\.py:\d[\d,-]*`")
#: Node id de pytest: `tests/unit/test_x.py::TestA::test_b`.
_NODE_ID = re.compile(
    r"`((?:tests|scripts|backend|worker)/[A-Za-z0-9_./-]+\.py)((?:::[A-Za-z_]\w*)+)`"
)


@cache
def defined_symbols(path: Path) -> frozenset[str]:
    """Nombres cualificados que define el módulo: defs, clases, campos y constantes."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + node.name
                names.add(qualified)
                visit(node.body, qualified + ".", isinstance(node, ast.ClassDef))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and (in_class or not prefix):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(prefix + target.id)

    visit(tree.body, "", in_class=False)
    return frozenset(names)


def _resolves(symbol: str, names: frozenset[str]) -> bool:
    if symbol in names:
        return True
    return "." not in symbol and any(n.endswith("." + symbol) for n in names)


def check_file(doc: Path) -> list[str]:
    problems: list[str] = []
    text = doc.read_text(encoding="utf-8")

    for label, rel_path, line_anchor in _LINK.findall(text):
        where = f"{doc.name}: [{label}] → {rel_path}"
        if line_anchor:
            problems.append(f"{where}{line_anchor} cita por línea: referenciá por símbolo")
            continue
        target = (doc.parent / rel_path).resolve()
        if not target.exists():
            problems.append(f"{where} no existe")
            continue
        names = defined_symbols(target)
        for symbol in _SYMBOL.findall(label):
            if symbol == target.name:
                continue  # la etiqueta nombra el archivo, no un símbolo
            if not _resolves(symbol, names):
                problems.append(f"{where} no define '{symbol}'")

    for ref in _LINE_REF.findall(text):
        problems.append(f"{doc.name}: {ref} cita por línea: referenciá por símbolo")

    for rel_path, node_path in _NODE_ID.findall(text):
        target = REPO_ROOT / rel_path
        if not target.exists():
            problems.append(f"{doc.name}: `{rel_path}{node_path}` → {rel_path} no existe")
            continue
        symbol = node_path.removeprefix("::").replace("::", ".")
        if symbol not in defined_symbols(target):
            problems.append(f"{doc.name}: `{rel_path}{node_path}` no define '{symbol}'")

    return problems


def main(argv: list[str]) -> int:
    docs = [Path(a) for a in argv[1:]] or sorted((REPO_ROOT / "docs").glob("*.md"))
    problems: list[str] = []
    for doc in docs:
        problems.extend(check_file(doc))

    if problems:
        print(f"Referencias rotas ({len(problems)}):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nCitá por símbolo: [`funcion`](../ruta/archivo.py) o "
            "`tests/ruta/test_x.py::test_nombre`, sin números de línea.",
            file=sys.stderr,
        )
        return 1

    checked = sum(
        len(_LINK.findall(t)) + len(_NODE_ID.findall(t))
        for t in (d.read_text(encoding="utf-8") for d in docs)
    )
    print(f"{checked} referencias verificadas en {len(docs)} documento(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
