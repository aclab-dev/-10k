"""Tests del verificador de referencias de documentación (`scripts/check_doc_anchors.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_doc_anchors
from scripts.check_doc_anchors import check_file

_MODULE = '''"""Módulo de prueba."""

LIMIT = 3


def alpha() -> int:
    total = 1
    return total


class Beta:
    field: int = 0

    def gamma(self) -> None:
        local = 1
        del local

    def shared(self) -> None:
        pass


class Delta:
    def shared(self) -> None:
        pass
'''


def _fixture(tmp_path: Path, doc_body: str) -> Path:
    code_dir = tmp_path / "backend"
    code_dir.mkdir()
    (code_dir / "sample.py").write_text(_MODULE, encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "d.md"
    doc.write_text(doc_body, encoding="utf-8")
    return doc


def test_accepts_function_symbol(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py)")
    assert check_file(doc) == []


def test_accepts_qualified_method_and_field(tmp_path: Path) -> None:
    doc = _fixture(
        tmp_path, "[`Beta.gamma`](../backend/sample.py) y [`Beta.field`](../backend/sample.py)"
    )
    assert check_file(doc) == []


def test_accepts_module_constant(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`LIMIT`](../backend/sample.py)")
    assert check_file(doc) == []


def test_accepts_unqualified_member_name(tmp_path: Path) -> None:
    """`gamma` suelto resuelve contra `Beta.gamma`: la etiqueta puede omitir la clase."""
    doc = _fixture(tmp_path, "[`gamma`](../backend/sample.py)")
    assert check_file(doc) == []


def test_rejects_ambiguous_unqualified_member_name(tmp_path: Path) -> None:
    """`shared` existe en `Beta` y en `Delta`: suelto no dice a cuál apunta la evidencia."""
    doc = _fixture(tmp_path, "[`shared`](../backend/sample.py)")
    problems = check_file(doc)
    assert len(problems) == 1
    assert "ambiguo" in problems[0]
    assert "Beta.shared" in problems[0] and "Delta.shared" in problems[0]


def test_accepts_qualified_name_that_would_be_ambiguous_unqualified(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`Delta.shared`](../backend/sample.py)")
    assert check_file(doc) == []


def test_accepts_file_name_label(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`sample.py`](../backend/sample.py)")
    assert check_file(doc) == []


def test_every_symbol_in_label_is_verified(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`/`delta`](../backend/sample.py)")
    problems = check_file(doc)
    assert len(problems) == 1
    assert "no define 'delta'" in problems[0]


def test_rejects_undefined_symbol(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`omega`](../backend/sample.py)")
    assert "no define 'omega'" in check_file(doc)[0]


def test_rejects_wrong_qualifier(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`Beta.alpha`](../backend/sample.py)")
    assert "no define 'Beta.alpha'" in check_file(doc)[0]


def test_function_locals_are_not_symbols(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`total`](../backend/sample.py)")
    assert "no define 'total'" in check_file(doc)[0]


def test_rejects_line_anchor(tmp_path: Path) -> None:
    """El caso real: un import nuevo corre el ancla en silencio. No se admite."""
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L6)")
    problems = check_file(doc)
    assert len(problems) == 1
    assert "cita por línea" in problems[0]


def test_rejects_line_reference_in_text(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "wired en `engine.py:146`, test `tests/unit/test_x.py:23-172`.")
    problems = check_file(doc)
    assert len(problems) == 2
    assert all("cita por línea" in p for p in problems)


def test_rejects_missing_file(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/nope.py)")
    assert "no existe" in check_file(doc)[0]


def test_prose_label_only_requires_the_file(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[docstring del módulo](../backend/sample.py)")
    assert check_file(doc) == []


@pytest.mark.parametrize(
    ("node_id", "ok"),
    [
        ("backend/sample.py::alpha", True),
        ("backend/sample.py::Beta::gamma", True),
        ("backend/sample.py::Beta::omega", False),
        ("backend/nope.py::alpha", False),
    ],
)
def test_verifies_pytest_node_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node_id: str, ok: bool
) -> None:
    doc = _fixture(tmp_path, f"Test: `{node_id}`.")
    monkeypatch.setattr(check_doc_anchors, "REPO_ROOT", tmp_path)
    assert (check_file(doc) == []) is ok


def test_repo_docs_have_no_broken_references() -> None:
    """El documento real: es el gate de la regla 34 y tiene que verificar."""
    repo_docs = Path(__file__).resolve().parents[2] / "docs"
    problems = [p for doc in sorted(repo_docs.glob("*.md")) for p in check_file(doc)]
    assert problems == [], problems
