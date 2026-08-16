"""Tests for the structure-aware prose chunker (P0 prototype).

Covers frontmatter handling, heading-hierarchy sectioning, atomic code/table
preservation, parent-child linkage, token bounding, small-section merging, and
line-range fidelity. Hermetic — no dependency on the sample corpus.
"""

import pytest

from knowcode.indexing.prose_chunker import (
    ProseChunker,
    ProseChunkingConfig,
    _blocks_in_span,
    _build_sections,
    _split_frontmatter,
)

# tiktoken decode round-trips are not byte-exact at token boundaries, so a
# token-window split can re-count one token over budget. Allow a tiny slack.
_DRIFT = 2


def _reconstruct(text: str, chunk) -> str:
    lines = text.splitlines()
    return "\n".join(lines[chunk.start_line - 1 : chunk.end_line]).strip("\n")


def test_frontmatter_parsed_with_correct_body_offset():
    text = "---\ntitle: My PRD\ntype: prd\n---\n# Heading\nBody line.\n"
    meta, body_start = _split_frontmatter(text.splitlines())
    assert meta == {"title": "My PRD", "type": "prd"}
    assert text.splitlines()[body_start - 1] == "# Heading"


def test_malformed_frontmatter_is_skipped_not_crashed():
    text = "---\ntitle: : : bad\n  - nope\n---\nBody.\n"
    meta, body_start = _split_frontmatter(text.splitlines())
    assert meta == {}
    assert text.splitlines()[body_start - 1] == "Body."


def test_doc_without_frontmatter_starts_at_line_one():
    meta, body_start = _split_frontmatter(["# Title", "text"])
    assert meta == {}
    assert body_start == 1


def test_heading_hierarchy_builds_breadcrumb_paths():
    text = "# A\nintro\n## B\nbody\n### C\ndeep\n"
    sections = _build_sections(text.splitlines(), 1, "doc.md", "Doc")
    paths = {s.heading_path for s in sections}
    assert ("A",) in paths
    assert ("A", "B") in paths
    assert ("A", "B", "C") in paths


def test_parent_child_linkage_follows_nesting():
    text = "# A\nx\n## B\ny\n"
    sections = {
        s.heading_path: s for s in _build_sections(text.splitlines(), 1, "d.md", "D")
    }
    a = sections[("A",)]
    b = sections[("A", "B")]
    assert a.parent_id == "d.md::sec::_root"
    assert b.parent_id == a.section_id


def test_heading_inside_code_fence_is_not_a_section():
    text = "# Real\n```\n# fake heading in code\n```\nafter\n"
    sections = _build_sections(text.splitlines(), 1, "d.md", "D")
    titles = [s.title for s in sections if s.level > 0]
    assert titles == ["Real"]


def test_code_block_kept_atomic_when_it_fits():
    lines = ["```python", "def f():", "    return 1", "```"]
    blocks = _blocks_in_span(lines, 1, len(lines))
    assert len(blocks) == 1
    assert blocks[0].kind == "code"
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 4)


def test_table_kept_as_single_block():
    lines = ["| a | b |", "| - | - |", "| 1 | 2 |"]
    blocks = _blocks_in_span(lines, 1, len(lines))
    assert len(blocks) == 1 and blocks[0].kind == "table"


def test_context_header_includes_title_and_path():
    text = "# Requirements\n## Rate Limiting\nThe cap is five retries.\n"
    chunks = ProseChunker().chunk_text(text, "prd/atlas.md")
    leaf = next(
        c for c in chunks if c.heading_path == ("Requirements", "Rate Limiting")
    )
    assert leaf.context_header == "atlas > Requirements > Rate Limiting"
    assert leaf.embedding_text.startswith(leaf.context_header)


def test_line_ranges_reconstruct_chunk_content():
    text = (
        "---\ntitle: Doc\n---\n"
        "# A\nAlpha paragraph one.\n\nAlpha paragraph two.\n"
        "## B\nBeta body here.\n"
    )
    chunks = ProseChunker().chunk_text(text, "d.md")
    assert chunks
    for c in chunks:
        assert _reconstruct(text, c) == c.content


def test_long_document_chunks_are_bounded():
    cfg = ProseChunkingConfig(target_tokens=40, max_tokens=64)
    body = " ".join(f"Sentence number {i} has several words." for i in range(200))
    chunks = ProseChunker(cfg).chunk_text(f"# Doc\n{body}\n", "d.md")
    assert len(chunks) > 1
    assert max(c.token_count for c in chunks) <= cfg.max_tokens + _DRIFT


def test_sentenceless_blob_is_bounded_and_flagged():
    cfg = ProseChunkingConfig(target_tokens=40, max_tokens=64)
    blob = "https://example.com/a/" + "x" * 4000  # no sentence boundaries
    chunks = ProseChunker(cfg).chunk_text(f"# Links\n{blob}\n", "d.md")
    assert len(chunks) > 1
    assert all(c.token_count <= cfg.max_tokens + _DRIFT for c in chunks)
    assert any(c.is_oversize for c in chunks)


def test_small_adjacent_sections_merge():
    cfg = ProseChunkingConfig(min_tokens=200, max_tokens=512)
    text = "# A\nshort a\n## B\nshort b\n## C\nshort c\n"
    big = ProseChunker(cfg).chunk_text(text, "d.md")
    small = ProseChunker(ProseChunkingConfig(min_tokens=0)).chunk_text(text, "d.md")
    assert len(big) < len(small)  # merging reduced the chunk count


def test_pure_parent_heading_emits_no_chunk():
    text = "# Parent\n## Child\nonly child has body\n"
    chunks = ProseChunker().chunk_text(text, "d.md")
    assert all(c.heading_path != ("Parent",) for c in chunks)
    assert any(c.heading_path == ("Parent", "Child") for c in chunks)


@pytest.mark.parametrize("text", ["", "   \n  \n", "---\ntitle: Empty\n---\n"])
def test_empty_documents_yield_no_chunks(text):
    assert ProseChunker().chunk_text(text, "d.md") == []
