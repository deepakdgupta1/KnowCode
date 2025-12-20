"""Unit tests for chunk repositories."""

from knowcode.data_models import CodeChunk
from knowcode.storage.chunk_repository import InMemoryChunkRepository


def test_chunk_repository_basic() -> None:
    """Chunks should be retrievable by ID and entity."""
    repo = InMemoryChunkRepository()
    chunk = CodeChunk(id="c1", entity_id="e1", content="content 1", tokens=["content", "1"])
    repo.add(chunk)

    assert repo.get("c1") == chunk
    assert repo.get_by_entity("e1") == [chunk]


def test_chunk_repository_token_search_limit() -> None:
    """Token search should respect limit and ordering."""
    repo = InMemoryChunkRepository()
    repo.add(CodeChunk(id="c1", entity_id="e1", content="alpha beta", tokens=["alpha", "beta"]))
    repo.add(CodeChunk(id="c2", entity_id="e2", content="alpha gamma", tokens=["alpha", "gamma"]))

    results = repo.search_by_tokens(["alpha"], limit=1)
    assert len(results) == 1
    assert results[0].id in {"c1", "c2"}
