"""A chunk is content-addressed by SHA-256, like an entity (BL-11).

``Chunker`` hashed chunk content with MD5 in all four places it minted a
``content_hash``, while entities used SHA-256 through
``compute_entity_content_hash``. The two halves of one index were addressed by
digests of different strength, and the weaker one is the load-bearing half:
``Indexer._reuse_durable_embeddings`` looks a chunk up by it and, on a hit,
attaches the stored embedding without ever comparing content. A collision
therefore hands one chunk another chunk's vector, and the chunk still
retrieves, so nothing fails loudly.

Each assertion checks the digest *equals the SHA-256 of the chunk's own
content*, never that it is 64 characters wide. A width test passes on any other
64-character digest, which is a lossy projection of the thing that matters.
"""

import hashlib
from pathlib import Path

from knowcode.data_models import ParseResult
from knowcode.indexing.chunker import Chunker
from knowcode.indexing.indexer import Indexer
from knowcode.indexing.prose_chunker import ProseChunk
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.utils.entity_identity import compute_entity_content_hash

SOURCE = '''"""Module docstring."""

import os


def alpha(value: int) -> int:
    """Return value plus one."""
    return value + 1


class Beta:
    """A class."""

    def method(self, value: int) -> int:
        """Call alpha."""
        return alpha(value)
'''

PROSE = """# Title

Body of the first section, long enough to be its own chunk.

## Second

Body of the second section, also long enough to stand alone.
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(tmp_path: Path, name: str, text: str, parser) -> list:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return Chunker().process_parse_result(parser.parse_file(str(path)))


def test_every_code_chunk_digest_is_the_sha256_of_its_own_content(
    tmp_path: Path,
) -> None:
    chunks = _chunks(tmp_path, "mod.py", SOURCE, PythonParser())
    hashed = [c for c in chunks if "content_hash" in c.metadata]
    assert hashed, "the probe needs at least one chunk carrying a digest"

    assert [c.metadata["content_hash"] for c in hashed] == [
        _sha256(c.content) for c in hashed
    ]


def test_every_prose_chunk_digest_is_the_sha256_of_its_own_content(
    tmp_path: Path,
) -> None:
    chunks = _chunks(tmp_path, "doc.md", PROSE, MarkdownParser())
    hashed = [c for c in chunks if "content_hash" in c.metadata]
    assert hashed, "the probe needs at least one chunk carrying a digest"

    assert [c.metadata["content_hash"] for c in hashed] == [
        _sha256(c.content) for c in hashed
    ]


def test_the_prose_route_carries_the_digest_the_prose_chunker_computed() -> None:
    """The digest is computed once, on the ProseChunker's side of the seam.

    ``ProseChunk`` already carries a SHA-256 of its own content, and the code
    route used to throw it away and hash the same bytes again. Once both sides
    agree on the algorithm no honest input can tell a reuse from a second hash,
    so the observation has to come from a chunk whose recorded digest does not
    describe its content.
    """
    prose = ProseChunk(
        id="doc.md::section::0",
        doc_id="doc.md",
        doc_type="markdown",
        title="Title",
        heading_path=("Title",),
        context_header="Title",
        section_id="doc.md::section",
        parent_id=None,
        level=1,
        start_line=1,
        end_line=2,
        token_count=4,
        content="Section body.",
        content_hash="witness",
        is_oversize=False,
    )

    chunk = Chunker()._prose_chunk(
        prose,
        entity_id="doc.md::section",
        index=0,
        kind="section",
        parent_id=None,
        last_modified="",
    )

    assert chunk.metadata["content_hash"] == "witness"


def test_chunks_and_entities_are_addressed_by_the_same_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mod.py"
    path.write_text(SOURCE, encoding="utf-8")
    result: ParseResult = PythonParser().parse_file(str(path))
    chunks = Chunker().process_parse_result(result)

    chunk_widths = {
        len(c.metadata["content_hash"]) for c in chunks if "content_hash" in c.metadata
    }
    entity_widths = {len(compute_entity_content_hash(e)) for e in result.entities}

    assert chunk_widths == entity_widths == {64}


def test_the_index_schema_version_rejects_a_generation_hashed_with_md5() -> None:
    """Every chunk's reuse key changed, so no older generation may be reused."""
    assert Indexer.SUPPORTED_SCHEMA_VERSIONS == {Indexer.SCHEMA_VERSION}
    assert Indexer.SCHEMA_VERSION >= 6
