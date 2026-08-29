"""Prose files are chunked as prose, not by the Python header extractor.

`Chunker._emit_module_chunks` ran for every file with source, and its
`_extract_module_header` stops at the first line starting with `import `,
`from `, `class ` or `def `. That rule is right for Python and arbitrary for
markdown, where those prefixes occur inside fences and in ordinary sentences,
so everything after such a line was silently never chunked. Where the extractor
did not trip, the opposite happened: the whole document became one chunk,
because `_emit_module_chunks` never applies the sliding window.

`ProseChunker` already splits on the heading hierarchy with hard token bounds.
These assert it is what runs for `.md` and `.rst`, that its output joins the
graph, and that code files are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import EntityKind
from knowcode.indexing.chunker import Chunker
from knowcode.indexing.prose_chunker import ProseChunkingConfig
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.rst_parser import RstParser
from knowcode.utils.token_counter import TokenCounter

# A fenced block whose first line trips the Python header extractor, followed by
# most of the document's bytes. This is the shape section 6.2 measured.
TRAPPED_MARKDOWN = """# Setup Guide

Install it, then wire the client.

```python
from acme import Client

client = Client(token="...")
```

## Configuration

{tail}

## Troubleshooting

If the client raises `AuthError`, the token is stale. Rotate it in the console
and restart the worker pool. The cached credential lives for one hour.
"""


def _markdown(tmp_path: Path, text: str, name: str = "setup-guide.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _coverage(path: Path, chunks) -> float:
    """Fraction of the file's bytes that reach chunk content."""
    return sum(len(chunk.content) for chunk in chunks) / len(
        path.read_text(encoding="utf-8")
    )


def test_a_fenced_import_line_does_not_truncate_the_document(tmp_path):
    tail = "\n\n".join(
        f"Paragraph {n} explains one configuration key in enough words to be "
        f"worth retrieving on its own."
        for n in range(12)
    )
    path = _markdown(tmp_path, TRAPPED_MARKDOWN.format(tail=tail))

    chunks = Chunker().process_parse_result(MarkdownParser().parse_file(path))

    assert _coverage(path, chunks) >= 0.95


def test_no_chunk_exceeds_the_configured_token_budget(tmp_path):
    """Section 6.2's other half. Where the header extractor did not trip, the
    entire document became one chunk, because `_emit_module_chunks` never
    applies a window. A chunk the chunker declares oversize is exempt: that is
    a single atomic block it was not allowed to split."""
    # No line here starts with import, from, class or def, so the old header
    # extractor never stopped and swallowed the document whole.
    body = "\n\n".join(
        f"Paragraph {n} explains one configuration key in enough words to be "
        f"worth retrieving on its own."
        for n in range(40)
    )
    path = _markdown(tmp_path, f"# Untrapped\n\n{body}\n", name="untrapped.md")
    budget = ProseChunkingConfig().max_tokens
    counter = TokenCounter()

    chunks = Chunker().process_parse_result(MarkdownParser().parse_file(path))

    assert len(chunks) > 1
    unbounded = [
        chunk.id
        for chunk in chunks
        if counter.count_tokens(chunk.content) > budget
        and "oversize" not in chunk.metadata
    ]
    assert unbounded == []


def test_no_chunk_is_emitted_for_a_section_entity(tmp_path):
    """B3. A heading with no body was stored as a chunk holding its own label.

    Length is not the tell; a real section body can be one short sentence. The
    tell is content that is exactly the entity's name, which is what
    `_chunk_entity` falls back to when an entity carries no source.
    """
    text = (
        "# Manual\n\nAn intro paragraph with enough words to be worth storing.\n\n"
        "## Empty Parent\n\n"  # a pure parent: heading, no body of its own
        "### Real Child\n\nThe child paragraph carries the actual content.\n"
    )
    path = _markdown(tmp_path, text, name="manual.md")
    result = MarkdownParser().parse_file(path)
    names = {entity.id: entity.name for entity in result.entities}

    chunks = Chunker().process_parse_result(result)

    assert chunks
    label_only = [
        chunk for chunk in chunks if chunk.content.strip() == names.get(chunk.entity_id)
    ]
    assert label_only == []


def test_every_prose_chunk_points_at_an_entity_that_exists(tmp_path):
    """BL-6. A chunk whose entity_id resolves to nothing is off the graph."""
    path = _markdown(tmp_path, TRAPPED_MARKDOWN.format(tail="Body text here."))
    result = MarkdownParser().parse_file(path)
    known = {entity.id for entity in result.entities}

    chunks = Chunker().process_parse_result(result)

    assert chunks
    dangling = sorted({c.entity_id for c in chunks} - known)
    assert dangling == []


def test_a_prose_chunk_carries_its_prose_metadata(tmp_path):
    """B4. Prose lands in the same table, tagged, with its structure kept."""
    path = _markdown(tmp_path, TRAPPED_MARKDOWN.format(tail="Body text here."))

    chunks = Chunker().process_parse_result(MarkdownParser().parse_file(path))

    assert chunks
    for chunk in chunks:
        assert chunk.metadata["type"] == "prose"
        assert chunk.metadata["content_hash"]
        assert chunk.metadata["context_header"]
        assert chunk.metadata["line_range"]


def test_a_prose_chunk_line_range_resolves_to_its_own_content(tmp_path):
    """The source stays authoritative, so the pointer back to it must be right."""
    path = _markdown(tmp_path, TRAPPED_MARKDOWN.format(tail="Body text here."))
    lines = path.read_text(encoding="utf-8").splitlines()

    chunks = Chunker().process_parse_result(MarkdownParser().parse_file(path))

    assert chunks
    for chunk in chunks:
        start, end = (int(part) for part in chunk.metadata["line_range"].split("-"))
        span = "\n".join(lines[start - 1 : end])
        assert chunk.content.strip() in span


@pytest.mark.parametrize(
    "name, text",
    [
        ("guide.md", "# Guide\n\nfrom acme import Client\n\nMore prose here.\n"),
        ("guide.rst", "Guide\n=====\n\nfrom acme import Client\n\nMore prose.\n"),
    ],
)
def test_prose_files_get_no_module_or_imports_chunk(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    parser = MarkdownParser() if name.endswith(".md") else RstParser()

    chunks = Chunker().process_parse_result(parser.parse_file(path))

    kinds = {chunk.metadata.get("type") for chunk in chunks}
    assert "module_header" not in kinds
    assert "imports" not in kinds


def test_code_files_still_get_their_module_and_imports_chunks(tmp_path):
    path = tmp_path / "worker.py"
    path.write_text(
        '"""Worker module."""\n\nimport os\n\n\ndef run():\n    return os.getpid()\n',
        encoding="utf-8",
    )

    chunks = Chunker().process_parse_result(PythonParser().parse_file(path))

    kinds = {chunk.metadata.get("type") for chunk in chunks}
    assert "module_header" in kinds
    assert "imports" in kinds
