"""The storage plan's code anchor index names symbols that exist (BL-5).

Appendix A of `docs/research/storage_optimization_2026_v4.md` used to cite line
numbers. They drifted by roughly 250 lines while the prose beside them stayed
true, so a reader who trusted a number landed in the wrong function. The index
now names symbols, and this test is what keeps that claim honest: a rename or a
deletion turns the index red instead of leaving it quietly wrong.

It deliberately does not check the descriptions. A test cannot read prose, and
pretending otherwise would be the same false assurance the line numbers gave.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PLAN = Path(__file__).parents[3] / "docs/research/storage_optimization_2026_v4.md"
SOURCE_ROOT = Path(__file__).parents[3] / "src/knowcode"
REPO_ROOT = Path(__file__).parents[3]

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(?:`([^`]+)`|—)\s*\|")


def _anchor_rows() -> list[tuple[str, str | None]]:
    body = PLAN.read_text(encoding="utf-8")
    start = body.index("## 14. Appendix A: Code Anchor Index")
    end = body.index("## 15. Appendix B: Schema Deltas")
    rows: list[tuple[str, str | None]] = []
    for line in body[start:end].splitlines():
        match = _ROW.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def _defined_names(path: Path) -> set[str]:
    """Every dotted class, function, and module-level assignment in a file."""
    names: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(f"{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name):
                    names.add(f"{prefix}{child.target.id}")
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(f"{prefix}{target.id}")

    walk(ast.parse(path.read_text(encoding="utf-8")), "")
    return names


ROWS = _anchor_rows()


def test_the_index_was_actually_read() -> None:
    """An empty parse and a clean index are indistinguishable without this."""
    assert len(ROWS) > 20, f"parsed {len(ROWS)} anchor rows"


def test_the_index_cites_no_line_numbers() -> None:
    body = PLAN.read_text(encoding="utf-8")
    start = body.index("## 14. Appendix A: Code Anchor Index")
    end = body.index("## 15. Appendix B: Schema Deltas")
    offenders = re.findall(r"`[^`]+\.py:\d+`", body[start:end])
    assert offenders == [], f"line anchors are back in Appendix A: {offenders}"


@pytest.mark.parametrize("path, symbol", ROWS, ids=lambda v: str(v))
def test_every_anchor_names_a_symbol_that_exists(path: str, symbol: str | None) -> None:
    source = (REPO_ROOT if path.startswith("scripts/") else SOURCE_ROOT) / path
    assert source.exists(), f"{path} is gone"
    if symbol is None:
        return
    assert symbol in _defined_names(source), f"{path} no longer defines {symbol}"
