"""Tests for exact query engine."""

from knowcode.retrieval.exact_query_engine import ExactQueryEngine
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.data_models import CodeChunk


def test_exact_query_engine_search() -> None:
    repo = SqliteChunkRepository(":memory:")
    chunk1 = CodeChunk(id="c1", entity_id="e1", content="def hello_world(): pass")
    chunk2 = CodeChunk(id="c2", entity_id="e2", content="def goodbye_world(): pass")
    repo.add(chunk1)
    repo.add(chunk2)

    engine = ExactQueryEngine(repo)

    # Exact match for "hello_world"
    results = engine.search_scored('"hello_world"', limit=10, expand_deps=False)
    assert len(results) == 1
    assert results[0].chunk.id == "c1"
    assert results[0].score == 1.0

    # Unquoted match for hello_world
    results = engine.search_scored("hello_world", limit=10, expand_deps=False)
    assert len(results) == 1
    assert results[0].chunk.id == "c1"

    # Exact match for missing
    results = engine.search_scored('"missing_func"', limit=10, expand_deps=False)
    assert len(results) == 0


def test_exact_query_engine_extracts_quotes() -> None:
    repo = SqliteChunkRepository(":memory:")
    chunk = CodeChunk(id="c1", entity_id="e1", content='print("hello world")')
    repo.add(chunk)

    engine = ExactQueryEngine(repo)
    # The quotes should be stripped for the actual search term
    results = engine.search_scored('"hello world"', limit=10, expand_deps=False)
    assert len(results) == 1
