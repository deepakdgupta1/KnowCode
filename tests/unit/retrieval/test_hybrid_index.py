"""Unit tests for hybrid retrieval scoring."""

from knowcode.data_models import CodeChunk
from knowcode.retrieval.hybrid_index import HybridIndex


class StubRepo:
    def __init__(self, chunks):
        self._chunks = {c.id: c for c in chunks}
        self._sparse = chunks

    def search_by_tokens(self, _tokens, limit=10):
        return self._sparse[:limit]

    def get(self, chunk_id):
        return self._chunks.get(chunk_id)


class StubVectorStore:
    def __init__(self, results):
        self._results = results

    def search(self, _embedding, limit=10):
        return self._results[:limit]


def test_hybrid_index_alpha_zero_prefers_sparse() -> None:
    """Alpha=0 should rank purely by sparse results."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    vector_store = StubVectorStore([("c2", 0.9), ("c1", 0.8)])

    index = HybridIndex(repo, vector_store, alpha=0.0)
    results = index.search("a", [0.0], limit=2)

    assert results[0][0].id == "c1"


def test_hybrid_index_alpha_one_prefers_dense() -> None:
    """Alpha=1 should rank purely by dense results."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    vector_store = StubVectorStore([("c2", 0.9), ("c1", 0.8)])

    index = HybridIndex(repo, vector_store, alpha=1.0)
    results = index.search("a", [0.0], limit=2)

    assert results[0][0].id == "c2"
