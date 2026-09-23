"""Tests del verificador de anclas de documentación (`scripts/check_doc_anchors.py`)."""

from __future__ import annotations

from pathlib import Path

from scripts.check_doc_anchors import check_file

_MODULE = '''"""Módulo de prueba."""


def alpha() -> int:
    total = 1
    return total


class Beta:
    def gamma(self) -> None:
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


def test_accepts_anchor_on_the_definition_line(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L4)")
    assert check_file(doc) == []


def test_accepts_anchor_inside_the_function_body(tmp_path: Path) -> None:
    """Apuntar a una línea del cuerpo es legítimo: la definición que la contiene manda."""
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L6)")
    assert check_file(doc) == []


def test_accepts_qualified_symbol(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`Beta.gamma`](../backend/sample.py#L10)")
    assert check_file(doc) == []


def test_rejects_anchor_pointing_at_another_symbol(tmp_path: Path) -> None:
    """El caso real: un import nuevo corre el ancla y apunta a otra función."""
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L10)")
    problems = check_file(doc)
    assert len(problems) == 1
    assert "no menciona 'alpha'" in problems[0]


def test_rejects_blank_target_line(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L2)")
    assert "línea vacía" in check_file(doc)[0]


def test_rejects_out_of_range_line(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/sample.py#L999)")
    assert "fuera de rango" in check_file(doc)[0]


def test_rejects_missing_file(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[`alpha`](../backend/nope.py#L1)")
    assert "no existe" in check_file(doc)[0]


def test_location_label_only_requires_a_non_blank_line(tmp_path: Path) -> None:
    """`archivo.py:NN` señala una línea a propósito; no se le exige símbolo."""
    doc = _fixture(tmp_path, "[`sample.py:5`](../backend/sample.py#L5)")
    assert check_file(doc) == []


def test_prose_label_is_not_treated_as_a_symbol(tmp_path: Path) -> None:
    doc = _fixture(tmp_path, "[docstring del módulo](../backend/sample.py#L1)")
    assert check_file(doc) == []


def test_repo_docs_have_no_broken_anchors() -> None:
    """El documento real: es el gate de la regla 34 y tiene que verificar."""
    repo_docs = Path(__file__).resolve().parents[2] / "docs"
    problems = [p for doc in sorted(repo_docs.glob("*.md")) for p in check_file(doc)]
    assert problems == [], problems
