"""Unit tests for LanceDB vector store."""

import pytest
import numpy as np

try:
    from knowcode.storage.lancedb_vector_store import LanceDBVectorStore
except ImportError:
    LanceDBVectorStore = None


@pytest.fixture
def vector_store(tmp_path):
    if LanceDBVectorStore is None:
        pytest.skip("lancedb not installed or module missing")
    return LanceDBVectorStore(dimension=2, path=str(tmp_path / "lancedb"))

def test_add_and_search(vector_store) -> None:
    vector_store.add("c1", [1.0, 0.0])
    vector_store.add("c2", [0.0, 1.0])
    
    results = vector_store.search([1.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0][0] == "c1"

def test_save_load_persistence(tmp_path) -> None:
    if LanceDBVectorStore is None:
        pytest.skip("lancedb not installed or module missing")
        
    store = LanceDBVectorStore(dimension=2, path=str(tmp_path / "db1"))
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])
    
    path = tmp_path / "saved_vectors"
    store.save(path)
    
    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    results = loaded.search([1.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0][0] == "c1"

def test_incremental_update(vector_store) -> None:
    vector_store.add("c1", [1.0, 0.0])
    results = vector_store.search([1.0, 0.0], limit=1)
    assert results[0][0] == "c1"
    
    vector_store.add("c2", [0.0, 1.0])
    results = vector_store.search([0.0, 1.0], limit=1)
    assert results[0][0] == "c2"

def test_recall_at_k(tmp_path) -> None:
    if LanceDBVectorStore is None:
        pytest.skip("lancedb not installed")
        
    np.random.seed(42)
    dim = 16
    vectors = np.random.randn(100, dim).astype("float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / norms
    
    store = LanceDBVectorStore(dimension=dim, path=str(tmp_path / "lancedb_recall"))
    for i, vec in enumerate(vectors):
        store.add(f"c{i}", vec.tolist())
        
    query = vectors[0].tolist()
    results = store.search(query, limit=5)
    
    assert results[0][0] == "c0"

def test_memory_profile(tmp_path) -> None:
    if LanceDBVectorStore is None:
        pytest.skip("lancedb not installed")
        
    import tracemalloc
    store = LanceDBVectorStore(dimension=128, path=str(tmp_path / "lancedb_mem"))
    
    vectors = np.random.randn(100, 128).astype("float32").tolist()
    
    tracemalloc.start()
    for i, vec in enumerate(vectors):
        store.add(f"c{i}", vec)
    store.search(vectors[0], limit=1)
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    assert peak < 20 * 1024 * 1024  # 20MB upper bound for 100 vectors overhead
