"""Core tests for v2.1 features."""

import pytest
from knowcode.models import CodeChunk, ChunkingConfig, EntityKind, Entity, Location
from knowcode.chunk_repository import InMemoryChunkRepository
from knowcode.tokenizer import tokenize_code
from knowcode.chunker import Chunker


def test_code_chunk_creation():
    chunk = CodeChunk(
        id="test::chunk::0",
        entity_id="test::entity",
        content="def foo(): pass"
    )
    assert chunk.id == "test::chunk::0"
    assert chunk.tokens == []
    assert chunk.embedding is None


def test_chunk_repository():
    repo = InMemoryChunkRepository()
    chunk = CodeChunk(id="c1", entity_id="e1", content="content 1", tokens=["content", "1"])
    repo.add(chunk)
    
    assert repo.get("c1") == chunk
    assert repo.get_by_entity("e1") == [chunk]
    
    # Search
    results = repo.search_by_tokens(["content"])
    assert len(results) == 1
    assert results[0] == chunk


def test_tokenizer():
    tokens = tokenize_code("myFunctionName_snake_case")
    assert "my" in tokens
    assert "function" in tokens
    assert "name" in tokens
    assert "snake" in tokens
    assert "case" in tokens


def test_chunker_module_extraction():
    chunker = Chunker()
    source = '"""Module docstring."""\nimport os\n\ndef foo(): pass'
    
    # Mocking a ParseResult/Entity structure for Chunker.process_parse_result
    # For now, let's test internal methods
    header = chunker._extract_module_header(source)
    assert '"""Module docstring."""' in header
    
    imports = chunker._extract_imports(source)
    assert "import os" in imports
