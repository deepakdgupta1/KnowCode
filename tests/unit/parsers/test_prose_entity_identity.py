"""Prose entity IDs must be unique within a file (BL-1).

`MarkdownParser` and `RstParser` both built a section ID from the heading slug
alone, so a document whose H1 slugified to its own filename collided with the
document entity, and two headings sharing a title collided with each other. The
chunker then emitted duplicate chunk IDs, `validate_prepared_chunks` rejected
the whole file, and the document was absent from the index.

ADR 1 requires a qualified name to carry its lexical scope. In a document that
scope is the heading path, so these assert the path is there as well as the
uniqueness it buys.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.indexing.chunker import Chunker
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.rst_parser import RstParser

# (parser, extension, renderer taking a list of (level, title) and returning text)
DIALECTS = [
    pytest.param(
        MarkdownParser,
        ".md",
        lambda headings: "\n".join(
            f"{'#' * level} {title}\n\nbody of {title}\n" for level, title in headings
        ),
        id="markdown",
    ),
    pytest.param(
        RstParser,
        ".rst",
        lambda headings: "\n".join(
            f"{title}\n{('=' if level == 1 else '-') * len(title)}\n\nbody of {title}\n"
            for level, title in headings
        ),
        id="rst",
    ),
]


def _parse(tmp_path: Path, parser_class, ext: str, text: str, stem: str = "roadmap"):
    path = tmp_path / f"{stem}{ext}"
    path.write_text(text, encoding="utf-8")
    return parser_class().parse_file(path)


def _duplicate_ids(ids) -> list[str]:
    return sorted(
        value for value, count in collections.Counter(ids).items() if count > 1
    )


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_an_h1_matching_the_filename_does_not_collide_with_the_document(
    tmp_path, parser_class, ext, render
):
    result = _parse(
        tmp_path, parser_class, ext, render([(1, "Roadmap")]), stem="roadmap"
    )

    assert _duplicate_ids(e.id for e in result.entities) == []

    document = next(e for e in result.entities if e.kind == EntityKind.DOCUMENT)
    section = next(e for e in result.entities if e.kind == EntityKind.SECTION)
    assert document.id != section.id


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_the_same_heading_title_under_different_parents_gets_distinct_ids(
    tmp_path, parser_class, ext, render
):
    text = render(
        [(1, "Alpha"), (2, "Decision"), (1, "Beta"), (2, "Decision")],
    )
    result = _parse(tmp_path, parser_class, ext, text)

    assert _duplicate_ids(e.id for e in result.entities) == []
    sections = [e for e in result.entities if e.kind == EntityKind.SECTION]
    assert len(sections) == 4


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_identical_sibling_headings_get_distinct_ids(
    tmp_path, parser_class, ext, render
):
    text = render([(1, "Guide"), (2, "Example"), (2, "Example"), (2, "Example")])
    result = _parse(tmp_path, parser_class, ext, text)

    assert _duplicate_ids(e.id for e in result.entities) == []
    examples = [e for e in result.entities if e.name == "Example"]
    assert len(examples) == 3
    assert len({e.id for e in examples}) == 3


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_a_section_qualified_name_carries_its_heading_path(
    tmp_path, parser_class, ext, render
):
    text = render([(1, "Guide"), (2, "Setup")])
    result = _parse(tmp_path, parser_class, ext, text, stem="manual")

    by_name = {e.name: e for e in result.entities}
    document = next(e for e in result.entities if e.kind == EntityKind.DOCUMENT)

    assert document.qualified_name == "manual"
    assert by_name["Guide"].qualified_name == "manual.guide"
    assert by_name["Setup"].qualified_name == "manual.guide.setup"
    assert by_name["Setup"].name == "Setup"


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_contains_relationships_still_follow_heading_nesting(
    tmp_path, parser_class, ext, render
):
    text = render([(1, "Guide"), (2, "Setup"), (1, "Reference")])
    result = _parse(tmp_path, parser_class, ext, text)

    by_name = {e.name: e for e in result.entities}
    document = next(e for e in result.entities if e.kind == EntityKind.DOCUMENT)
    contains = {
        (r.source_id, r.target_id)
        for r in result.relationships
        if r.kind == RelationshipKind.CONTAINS
    }

    assert (document.id, by_name["Guide"].id) in contains
    assert (by_name["Guide"].id, by_name["Setup"].id) in contains
    assert (document.id, by_name["Reference"].id) in contains


@pytest.mark.parametrize("parser_class, ext, render", DIALECTS)
def test_a_colliding_document_still_chunks_without_a_duplicate_chunk_id(
    tmp_path, parser_class, ext, render
):
    """The end of the BL-1 chain: duplicate chunk IDs are what rejected the file."""
    text = render([(1, "Roadmap"), (2, "Decision"), (1, "Later"), (2, "Decision")])
    result = _parse(tmp_path, parser_class, ext, text, stem="roadmap")

    chunks = Chunker().process_parse_result(result)

    assert chunks, "the document produced no chunks at all"
    assert _duplicate_ids(chunk.id for chunk in chunks) == []
