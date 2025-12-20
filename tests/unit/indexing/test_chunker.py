"""Unit tests for the chunker."""

from pathlib import Path

from knowcode.indexing.chunker import Chunker
from knowcode.models import ChunkingConfig, Entity, EntityKind, Location, ParseResult


def test_chunker_module_extraction() -> None:
    """Module header and imports should be extracted."""
    chunker = Chunker()
    source = '"""Module docstring."""\nimport os\n\ndef foo(): pass'

    header = chunker._extract_module_header(source)
    assert '"""Module docstring."""' in header

    imports = chunker._extract_imports(source)
    assert "import os" in imports


def test_chunker_metadata_has_docstring_and_last_modified(tmp_path: Path) -> None:
    """Chunk metadata should include docstring and timestamp flags."""
    source = '"""Module docstring."""\n\ndef foo():\n    """Doc."""\n    return 1\n'
    file_path = tmp_path / "mod.py"
    file_path.write_text(source, encoding="utf-8")

    module_entity = Entity(
        id=f"{file_path}::mod",
        kind=EntityKind.MODULE,
        name="mod",
        qualified_name="mod",
        location=Location(str(file_path), 1, 5),
        source_code=source,
    )
    func_entity = Entity(
        id=f"{file_path}::foo",
        kind=EntityKind.FUNCTION,
        name="foo",
        qualified_name="foo",
        location=Location(str(file_path), 3, 5),
        docstring="Doc.",
        signature="def foo()",
        source_code="def foo():\n    return 1\n",
    )

    result = ParseResult(
        file_path=str(file_path),
        entities=[module_entity, func_entity],
        relationships=[],
    )
    chunker = Chunker()
    chunks = chunker.process_parse_result(result)

    func_chunks = [c for c in chunks if c.entity_id == func_entity.id]
    assert func_chunks
    assert func_chunks[0].metadata["has_docstring"] == "true"
    assert "last_modified" in func_chunks[0].metadata


def test_chunker_overlap_chunking() -> None:
    """Large entities should be split into overlapping chunks."""
    content = "a" * 120
    entity = Entity(
        id="file.py::big",
        kind=EntityKind.FUNCTION,
        name="big",
        qualified_name="big",
        location=Location("file.py", 1, 10),
        source_code=content,
    )
    result = ParseResult(file_path="file.py", entities=[entity], relationships=[])

    chunker = Chunker(ChunkingConfig(max_chunk_size=50, overlap=10))
    chunks = chunker.process_parse_result(result)

    assert len(chunks) > 1
    assert chunks[0].metadata["chunk_index"] == "0"
