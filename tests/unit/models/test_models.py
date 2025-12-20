"""Unit tests for core models."""

from knowcode.models import CodeChunk


def test_code_chunk_defaults() -> None:
    """CodeChunk defaults should be empty tokens and no embedding."""
    chunk = CodeChunk(
        id="test::chunk::0",
        entity_id="test::entity",
        content="def foo(): pass",
    )

    assert chunk.id == "test::chunk::0"
    assert chunk.tokens == []
    assert chunk.embedding is None
