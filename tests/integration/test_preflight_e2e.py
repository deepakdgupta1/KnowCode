"""Integration test: end-to-end pre-flight assessment on a fixture codebase."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from knowcode.analysis.preflight import assess_codebase, PreflightReport
from knowcode.analysis.preflight_writer import (
    load_preflight_report,
    write_preflight_report,
)
from knowcode.indexing.graph_builder import GraphBuilder


@pytest.fixture
def fixture_codebase(tmp_path: Path) -> Path:
    """Create a small Python fixture codebase with known quality characteristics."""
    src = tmp_path / "src"
    src.mkdir()

    # Well-documented, well-named module
    (src / "calculator.py").write_text(
        textwrap.dedent('''\
        """Calculator module with basic arithmetic."""


        def add_numbers(a: int, b: int) -> int:
            """Add two numbers and return the result."""
            return a + b


        def subtract_numbers(a: int, b: int) -> int:
            """Subtract b from a and return the result."""
            return a - b


        class MathProcessor:
            """Processes mathematical operations."""

            def multiply(self, x: float, y: float) -> float:
                """Multiply two numbers."""
                return x * y

            def divide(self, x: float, y: float) -> float:
                """Divide x by y."""
                if y == 0:
                    raise ValueError("Cannot divide by zero")
                return x / y
        '''),
        encoding="utf-8",
    )

    # Poorly-documented module with bad naming
    (src / "utils.py").write_text(
        textwrap.dedent("""\
        def f(x):
            return x + 1

        def g(x, y):
            return x * y

        class D:
            pass
        """),
        encoding="utf-8",
    )

    return src


def test_preflight_e2e_on_fixture(fixture_codebase: Path) -> None:
    """Run pre-flight on the fixture and verify the report is reasonable."""
    builder = GraphBuilder()
    builder.build_from_directory(root_dir=fixture_codebase)

    report = assess_codebase(
        entities=builder.entities,
        relationships=builder.relationships,
        scanned_files=builder.scanned_files,
        parse_errors=builder.errors,
    )

    assert isinstance(report, PreflightReport)
    assert report.entity_count > 0
    assert report.scanned_file_count == 2  # calculator.py + utils.py
    assert len(report.dimensions) == 10
    assert 0.0 <= report.overall_score <= 1.0
    assert report.overall_grade in ("A", "B", "C", "D", "F")

    # Parse should succeed: both files are valid Python
    parse_dim = next(
        d for d in report.dimensions if d.dimension == "parse_success_rate"
    )
    assert parse_dim.score == 1.0

    # Documentation density: calculator.py is fully documented, utils.py is not.
    # Expected: ~50-60% documentation rate
    doc_dim = next(
        d for d in report.dimensions if d.dimension == "documentation_density"
    )
    assert 0.3 <= doc_dim.score <= 0.8

    # Language coverage: all Python → 100%
    lang_dim = next(d for d in report.dimensions if d.dimension == "language_coverage")
    assert lang_dim.score == 1.0


def test_preflight_report_persistence(tmp_path: Path) -> None:
    """Verify the report can be written and loaded back."""
    report_dict = {
        "overall_score": 0.73,
        "overall_grade": "B",
        "dimensions": [
            {
                "dimension": "parse_success_rate",
                "score": 1.0,
                "grade": "A",
                "detail": "ok",
            },
        ],
        "summary": "Test summary.",
        "recommendations": ["Add docstrings."],
    }

    path = write_preflight_report(report_dict, tmp_path)
    assert path.exists()
    assert path.name == "preflight_report.json"

    loaded = load_preflight_report(tmp_path)
    assert loaded is not None
    assert loaded["overall_score"] == 0.73
    assert loaded["overall_grade"] == "B"
    assert len(loaded["dimensions"]) == 1


def test_load_report_missing(tmp_path: Path) -> None:
    """Loading from a directory with no report returns None."""
    result = load_preflight_report(tmp_path)
    assert result is None


def test_preflight_via_service(fixture_codebase: Path) -> None:
    """Test the service.preflight() method end-to-end."""
    from knowcode.service import KnowCodeService

    service = KnowCodeService()
    report = service.preflight(directory=str(fixture_codebase))

    assert isinstance(report, dict)
    assert "overall_score" in report
    assert "dimensions" in report
    assert len(report["dimensions"]) == 10
    assert report["scanned_file_count"] == 2
