"""Unit tests for MockVectorStore fallback."""

import pytest
import numpy as np
from pathlib import Path
from knowcode.storage.vector_store import MockVectorStore


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


def test_mock_vector_store_removes_rows_by_id() -> None:
    """Step 11: removal deletes the row, so ntotal never counts a tombstone."""
    store = MockVectorStore(dimension=2)
    store.add_with_ids(
        np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
        np.array([7, 9], dtype="int64"),
    )

    removed = store.remove_ids(np.array([7], dtype="int64"))

    assert removed == 1
    assert store.ntotal == 1
    _, ids = store.search(np.array([[1.0, 0.0]], dtype="float32"), k=1)
    assert ids[0][0] == 9


def test_mock_vector_store_removing_an_absent_id_removes_nothing() -> None:
    """An unknown native ID is a no-op, matching faiss.IndexIDMap2."""
    store = MockVectorStore(dimension=2)
    store.add_with_ids(
        np.array([[1.0, 0.0]], dtype="float32"), np.array([1], dtype="int64")
    )

    assert store.remove_ids(np.array([42], dtype="int64")) == 0
    assert store.ntotal == 1


def test_mock_vector_store_reconstructs_by_id() -> None:
    """``reconstruct`` reads by assigned ID, not by row position."""
    store = MockVectorStore(dimension=2)
    store.add_with_ids(
        np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
        np.array([5, 6], dtype="int64"),
    )
    store.remove_ids(np.array([5], dtype="int64"))

    assert np.allclose(store.reconstruct(6), [0.0, 1.0])
    with pytest.raises(KeyError):
        store.reconstruct(5)


def test_mock_vector_store_search_on_empty_index_returns_no_ids() -> None:
    """An empty index yields empty score/ID arrays rather than padding."""
    store = MockVectorStore(dimension=2)

    scores, ids = store.search(np.array([[1.0, 0.0]], dtype="float32"), k=3)

    assert scores.size == 0
    assert ids.size == 0


def test_mock_vector_store_set_row_ids_rejects_a_length_mismatch() -> None:
    """Restored IDs must cover every loaded row exactly once."""
    store = MockVectorStore(dimension=2)
    store.add(np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"))

    with pytest.raises(ValueError, match="row id count"):
        store.set_row_ids([1])


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
