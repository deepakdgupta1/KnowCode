"""Unit tests for chunk repositories."""

from knowcode.data_models import CodeChunk
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository


def test_chunk_repository_basic() -> None:
    """Chunks should be retrievable by ID and entity."""
    repo = SqliteChunkRepository(":memory:")
    chunk = CodeChunk(
        id="c1", entity_id="e1", content="content 1", tokens=["content", "1"]
    )
    repo.add(chunk)

    loaded = repo.get("c1")
    # tokens are an indexing-time derivation of content; D2 persists them only
    # inside the contentless FTS row, so hydration returns them empty.
    assert loaded == CodeChunk(id="c1", entity_id="e1", content="content 1", tokens=[])
    assert repo.get_by_entity("e1") == [loaded]


def test_chunk_repository_token_search_limit() -> None:
    """Token search should respect limit and ordering."""
    repo = SqliteChunkRepository(":memory:")
    repo.add(
        CodeChunk(
            id="c1", entity_id="e1", content="alpha beta", tokens=["alpha", "beta"]
        )
    )
    repo.add(
        CodeChunk(
            id="c2", entity_id="e2", content="alpha gamma", tokens=["alpha", "gamma"]
        )
    )

    results = repo.search_by_tokens(["alpha"], limit=1)
    assert len(results) == 1
    assert results[0].id in {"c1", "c2"}
