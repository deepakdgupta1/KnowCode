"""Unit tests for MockVectorStore fallback."""

import pytest
import numpy as np
from pathlib import Path
from knowcode.storage.vector_store import MockVectorStore, VectorStore


def test_mock_vector_store_search() -> None:
    """MockVectorStore should perform basic cosine similarity search."""
    store = MockVectorStore(dimension=2)
    # Vectors: [1, 0] and [0, 1]
    store.add(np.array([[1.0, 0.0]], dtype="float32"))
    store.add(np.array([[0.0, 1.0]], dtype="float32"))
    
    # Query for [1, 0.1]
    query = np.array([[1.0, 0.1]], dtype="float32")
    scores, indices = store.search(query, k=2)
    
    assert indices[0][0] == 0  # [1, 0] should be closer
    assert scores[0][0] > scores[0][1]


def test_mock_vector_store_persistence(tmp_path: Path) -> None:
    """MockVectorStore should save and load vectors using .npy."""
    path = tmp_path / "test_vectors.npy"
    store = MockVectorStore(dimension=2)
    vecs = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    store.add(vecs[0:1])
    store.add(vecs[1:2])
    
    store.save(path)
    assert path.exists()
    
    new_store = MockVectorStore(dimension=2)
    new_store.load(path)
    
    assert new_store.ntotal == 2
    assert np.allclose(new_store.index, vecs)


def test_vector_store_fallback_logic(monkeypatch: pytest.MonkeyPatch) -> None:
    """VectorStore should use MockVectorStore when faiss is missing."""
    import knowcode.storage.vector_store as vs
    monkeypatch.setattr(vs, "faiss", None)
    
    store = vs.VectorStore(dimension=128)
    assert isinstance(store.index, MockVectorStore)
    
    # Test add/search via fallback
    store.add("c1", [1.0] * 128)
    results = store.search([1.0] * 128, limit=1)
    assert results[0][0] == "c1"
