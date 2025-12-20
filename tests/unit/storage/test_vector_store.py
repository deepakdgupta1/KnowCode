"""Unit tests for vector store persistence."""

import pytest

from knowcode.storage import vector_store
from knowcode.storage.vector_store import VectorStore


def test_vector_store_save_load(tmp_path) -> None:
    """Vector store should persist index and ID map when FAISS is available."""
    if vector_store.faiss is None:
        pytest.skip("faiss not installed")

    store = VectorStore(dimension=2)
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])

    path = tmp_path / "vectors"
    store.save(path)

    loaded = VectorStore(dimension=2, index_path=path)
    assert loaded.id_map
    results = loaded.search([1.0, 0.0], limit=1)
    assert results[0][0] == "c1"
