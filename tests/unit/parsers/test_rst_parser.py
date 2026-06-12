"""Unit tests for the reStructuredText parser (DOCUMENT/SECTION graph entities).

Mirrors the markdown parser's contract: one DOCUMENT entity per file, one
SECTION per heading, and CONTAINS relationships following adornment-level
nesting. Hermetic — uses tmp_path, no corpus dependency.
"""

from pathlib import Path

from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.rst_parser import RstParser


def _parse(tmp_path: Path, text: str, name: str = "doc.rst"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return RstParser().parse_file(path)


def test_creates_one_document_entity(tmp_path):
    result = _parse(tmp_path, "Title\n=====\n\nbody\n")
    docs = [e for e in result.entities if e.kind == EntityKind.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].name == "doc"


def test_creates_section_entity_per_heading(tmp_path):
    result = _parse(tmp_path, "Guide\n=====\n\nintro\n\nSetup\n-----\n\nsteps\n")
    sections = {e.name for e in result.entities if e.kind == EntityKind.SECTION}
    assert sections == {"Guide", "Setup"}


def test_section_nesting_uses_contains_relationships(tmp_path):
    result = _parse(tmp_path, "Guide\n=====\n\nintro\n\nSetup\n-----\n\nsteps\n")
    by_name = {e.name: e for e in result.entities}
    doc = next(e for e in result.entities if e.kind == EntityKind.DOCUMENT)
    contains = {
        (r.source_id, r.target_id)
        for r in result.relationships
        if r.kind == RelationshipKind.CONTAINS
    }
    assert (doc.id, by_name["Guide"].id) in contains
    assert (by_name["Guide"].id, by_name["Setup"].id) in contains


def test_levels_assigned_by_encounter_order(tmp_path):
    # '=' is encountered first (level 1); '-' second (level 2).
    result = _parse(tmp_path, "Guide\n=====\n\nintro\n\nSetup\n-----\n\nsteps\n")
    levels = {
        e.name: e.metadata["level"]
        for e in result.entities
        if e.kind == EntityKind.SECTION
    }
    assert levels == {"Guide": "1", "Setup": "2"}


def test_description_taken_from_first_paragraph(tmp_path):
    result = _parse(tmp_path, "Title\n=====\n\nThe intro paragraph.\n\nMore.\n")
    doc = next(e for e in result.entities if e.kind == EntityKind.DOCUMENT)
    assert doc.docstring == "The intro paragraph."


def test_heading_inside_literal_block_is_ignored(tmp_path):
    text = "Real\n====\n\nExample::\n\n    Fake\n    ====\n\nDone.\n"
    result = _parse(tmp_path, text)
    sections = [e.name for e in result.entities if e.kind == EntityKind.SECTION]
    assert sections == ["Real"]


def test_missing_file_returns_error_not_crash(tmp_path):
    result = RstParser().parse_file(tmp_path / "absent.rst")
    assert result.errors
    assert result.entities == []


def test_empty_file_yields_document_without_errors(tmp_path):
    result = _parse(tmp_path, "", name="empty.rst")
    assert result.errors == []
    assert [e.kind for e in result.entities] == [EntityKind.DOCUMENT]
