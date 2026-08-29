"""Unit tests for the chunker."""

from pathlib import Path

import pytest

from knowcode.indexing.chunker import Chunker
from knowcode.data_models import (
    ChunkingConfig,
    Entity,
    EntityKind,
    Location,
    ParseResult,
)
from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.yaml_parser import YamlParser


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


# BL-6. `_emit_module_chunks` tagged its chunks `<file>::module`, an id no
# parser emits, so the chunk carrying the largest block of a file's text was
# disconnected from the graph. Every path that treats `chunk.entity_id` as a
# graph handle silently did nothing with it.
CHUNKABLE = [
    pytest.param(
        PythonParser,
        ".py",
        '"""Worker."""\n\nimport os\n\n\ndef run():\n    """Go."""\n    return os.getpid()\n',
        id="python",
    ),
    pytest.param(
        JavaScriptParser,
        ".js",
        "// Worker.\nimport os from 'os';\n\nexport function run() { return os.cpus(); }\n",
        id="javascript",
    ),
    pytest.param(
        TypeScriptParser,
        ".ts",
        "// Worker.\nimport os from 'os';\n\nexport function run(): number { return 1; }\n",
        id="typescript",
    ),
    pytest.param(
        RustParser,
        ".rs",
        "//! Worker.\nuse std::env;\n\npub fn run() -> u32 { 1 }\n",
        id="rust",
    ),
    pytest.param(
        JavaParser,
        ".java",
        "import java.util.List;\n\nclass Worker { void run() {} }\n",
        id="java",
    ),
    pytest.param(
        YamlParser, ".yaml", "service:\n  name: worker\n  port: 8080\n", id="yaml"
    ),
    pytest.param(
        MarkdownParser, ".md", "# Worker\n\nIt runs the thing.\n", id="markdown"
    ),
]


@pytest.mark.parametrize("parser_class, ext, code", CHUNKABLE)
def test_no_chunk_points_at_an_entity_that_does_not_exist(
    tmp_path: Path, parser_class, ext, code
) -> None:
    source = tmp_path / f"worker{ext}"
    source.write_text(code, encoding="utf-8")
    result = parser_class().parse_file(source)
    known = {entity.id for entity in result.entities}

    chunks = Chunker().process_parse_result(result)

    assert chunks, "the file produced no chunks at all"
    assert sorted({chunk.entity_id for chunk in chunks} - known) == []


def test_module_chunks_hang_on_the_module_entity(tmp_path: Path) -> None:
    source = tmp_path / "worker.py"
    source.write_text(
        '"""Worker."""\n\nimport os\n\n\ndef run():\n    return os.getpid()\n',
        encoding="utf-8",
    )
    result = PythonParser().parse_file(source)
    module = next(e for e in result.entities if e.kind == EntityKind.MODULE)

    chunks = Chunker().process_parse_result(result)

    module_chunks = [
        chunk
        for chunk in chunks
        if chunk.metadata.get("type") in {"module_header", "imports"}
    ]
    assert len(module_chunks) == 2
    assert {chunk.entity_id for chunk in module_chunks} == {module.id}


def test_a_class_chunk_does_not_contain_its_method_bodies(tmp_path: Path) -> None:
    """B1. A class entity's source_code contains its methods' source, and each
    method is already its own entity with its own chunk, so every member body
    was stored twice."""
    source = (
        "class Worker:\n"
        '    """Runs jobs."""\n\n'
        "    limit = 10\n\n"
        "    def start(self):\n"
        "        return MARKER_START\n\n"
        "    def stop(self):\n"
        "        return MARKER_STOP\n"
    )
    path = tmp_path / "worker.py"
    path.write_text(source, encoding="utf-8")
    result = PythonParser().parse_file(path)
    cls = next(e for e in result.entities if e.kind == EntityKind.CLASS)

    chunks = Chunker().process_parse_result(result)

    class_chunks = [c for c in chunks if c.entity_id == cls.id]
    assert class_chunks
    body = "".join(c.content for c in class_chunks)
    assert "MARKER_START" not in body
    assert "MARKER_STOP" not in body
    # The shell is what a reader needs to recognise the class.
    assert "class Worker" in body
    assert "limit = 10" in body


def test_each_method_still_has_its_own_chunk(tmp_path: Path) -> None:
    source = (
        "class Worker:\n"
        "    def start(self):\n"
        "        return MARKER_START\n\n"
        "    def stop(self):\n"
        "        return MARKER_STOP\n"
    )
    path = tmp_path / "worker.py"
    path.write_text(source, encoding="utf-8")
    result = PythonParser().parse_file(path)

    chunks = Chunker().process_parse_result(result)

    text = "".join(c.content for c in chunks)
    assert "MARKER_START" in text
    assert "MARKER_STOP" in text


def test_a_class_with_no_extracted_members_keeps_its_whole_source(
    tmp_path: Path,
) -> None:
    """Trimming at the first member is only safe when members became entities."""
    source = 'class Config:\n    """Just data."""\n\n    RETRIES = MARKER_VALUE\n'
    path = tmp_path / "config.py"
    path.write_text(source, encoding="utf-8")
    result = PythonParser().parse_file(path)
    cls = next(e for e in result.entities if e.kind == EntityKind.CLASS)
    members = [
        e
        for e in result.entities
        if e is not cls
        and cls.location.line_start < e.location.line_start <= cls.location.line_end
    ]

    chunks = Chunker().process_parse_result(result)

    body = "".join(c.content for c in chunks if c.entity_id == cls.id)
    if not members:
        assert "MARKER_VALUE" in body
    else:
        assert "MARKER_VALUE" in "".join(c.content for c in chunks)


def test_no_chunk_is_emitted_for_an_entity_with_nothing_but_a_name(
    tmp_path: Path,
) -> None:
    """B3. A YAML key became a 9-byte chunk holding its own label, and paid a
    full-width vector for it: 463x amplification for no retrievable text."""
    path = tmp_path / "service.yaml"
    path.write_text("service:\n  name: worker\n  port: 8080\n", encoding="utf-8")
    result = YamlParser().parse_file(path)
    names = {entity.id: entity.name for entity in result.entities}

    chunks = Chunker().process_parse_result(result)

    label_only = [c for c in chunks if c.content.strip() == names.get(c.entity_id)]
    assert label_only == []
    # The file's text is still reachable through the chunk that carries it.
    assert "port: 8080" in "".join(c.content for c in chunks)
