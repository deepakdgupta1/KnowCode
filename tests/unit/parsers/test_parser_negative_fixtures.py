"""Negative syntax/error fixtures prove partial extraction is visible and deterministic.

A malformed source must never crash the parser or silently vanish. It must report
a visible error, produce deterministic output across repeated runs, and leave no
invalid relationship endpoint among whatever it did extract. These gates cover
the parser-invariant 1 requirement that a limitation is classified, never silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_parser import VueParser
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id

NEGATIVE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts" / "negative"

PARSER_BY_EXTENSION = {
    ".py": PythonParser,
    ".js": JavaScriptParser,
    ".ts": TypeScriptParser,
    ".vue": VueParser,
}


def _negative_sources() -> list[Path]:
    return sorted(path for path in NEGATIVE_ROOT.iterdir() if path.is_file())


def _parse_snapshot(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...], int]:
    parser = PARSER_BY_EXTENSION[path.suffix]()
    result = parser.parse_file(path)
    entities = tuple(entity.id for entity in result.entities)
    relationships = tuple(
        sorted((r.source_id, r.target_id, r.kind.value) for r in result.relationships)
    )
    return entities, relationships, len(result.errors)


@pytest.mark.parametrize("source", _negative_sources(), ids=lambda p: p.name)
def test_malformed_source_reports_a_visible_error(source: Path) -> None:
    """A malformed source surfaces at least one error instead of parsing clean."""
    parser = PARSER_BY_EXTENSION[source.suffix]()
    result = parser.parse_file(source)
    assert result.errors, f"{source.name} must report a parse error; got none"


@pytest.mark.parametrize("source", _negative_sources(), ids=lambda p: p.name)
def test_malformed_source_is_deterministic(source: Path) -> None:
    """Repeated parses of a malformed source yield identical output."""
    first = _parse_snapshot(source)
    second = _parse_snapshot(source)
    assert first == second, f"{source.name} produced non-deterministic output"


@pytest.mark.parametrize("source", _negative_sources(), ids=lambda p: p.name)
def test_malformed_source_leaves_no_invalid_endpoints(source: Path) -> None:
    """Whatever a malformed source extracts must still satisfy endpoint classification.

    Partial extraction (common for tree-sitter's fault-tolerant AST) or whole-file
    failure (Python) is acceptable; emitting a legacy ``ref::``/``type::`` endpoint
    or a dangling internal edge is not.
    """
    parser = PARSER_BY_EXTENSION[source.suffix]()
    result = parser.parse_file(source)

    for relationship in result.relationships:
        assert classify_endpoint_id(relationship.source_id) is not EndpointKind.INVALID, (
            f"{source.name}: invalid source endpoint {relationship.source_id!r}"
        )
        assert classify_endpoint_id(relationship.target_id) is not EndpointKind.INVALID, (
            f"{source.name}: invalid target endpoint {relationship.target_id!r}"
        )
