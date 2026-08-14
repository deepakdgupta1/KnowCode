"""Unit tests for hybrid retrieval scoring."""

from knowcode.data_models import CodeChunk
from knowcode.retrieval.hybrid_index import HybridIndex


class StubRepo:
    def __init__(self, chunks, sparse=None) -> None:  # type: ignore
        self._chunks = {c.id: c for c in chunks}
        self._sparse = list(sparse) if sparse is not None else list(chunks)
        self.get_calls: list[str] = []

    def search_by_tokens(self, _tokens, limit=10):  # type: ignore
        return self._sparse[:limit]

    def get(self, chunk_id):  # type: ignore
        self.get_calls.append(chunk_id)
        return self._chunks.get(chunk_id)


class StubVectorStore:
    def __init__(self, results) -> None:  # type: ignore
        self._results = results

    def search(self, _embedding, limit=10):  # type: ignore
        return self._results[:limit]


def test_hybrid_index_alpha_zero_prefers_sparse() -> None:
    """Alpha=0 should rank purely by sparse results."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    vector_store = StubVectorStore([("c2", 0.9), ("c1", 0.8)])

    index = HybridIndex(repo, vector_store, alpha=0.0)  # type: ignore
    results = index.search("a", [0.0], limit=2)

    assert results[0][0].id == "c1"


def test_hybrid_index_alpha_one_prefers_dense() -> None:
    """Alpha=1 should rank purely by dense results."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    vector_store = StubVectorStore([("c2", 0.9), ("c1", 0.8)])

    index = HybridIndex(repo, vector_store, alpha=1.0)  # type: ignore
    results = index.search("a", [0.0], limit=2)

    assert results[0][0].id == "c2"


# ----------------------------------------------------------------------
# One read snapshot per query, defensive de-duplication (Step 15, task 6)
# ----------------------------------------------------------------------


def test_a_stale_dense_id_does_not_consume_a_result_slot() -> None:
    """A vector id the chunk store cannot resolve must not cost a result.

    Before Step 15 the fused list was truncated to ``limit`` *before* chunks
    were materialized, so one unresolvable dense id silently shortened the
    answer — the exact split-brain the incremental generation contract removes.
    """
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    vector_store = StubVectorStore([("ghost", 0.99), ("c1", 0.8), ("c2", 0.7)])

    index = HybridIndex(repo, vector_store, alpha=1.0)  # type: ignore
    results = index.search("a", [0.0], limit=2)

    assert [chunk.id for chunk, _ in results] == ["c1", "c2"]


def test_duplicate_sparse_results_do_not_bias_fusion() -> None:
    """A repeated sparse hit must score once, not twice."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    duplicated = StubRepo([c1, c2], sparse=[c1, c1, c2])
    clean = StubRepo([c1, c2], sparse=[c1, c2])
    vector_store = StubVectorStore([("c2", 0.9)])

    duplicated_scores = dict(
        (chunk.id, score)
        for chunk, score in HybridIndex(duplicated, vector_store, alpha=0.5).search(  # type: ignore
            "a", [0.0], limit=5
        )
    )
    clean_scores = dict(
        (chunk.id, score)
        for chunk, score in HybridIndex(clean, vector_store, alpha=0.5).search(  # type: ignore
            "a", [0.0], limit=5
        )
    )

    assert duplicated_scores == clean_scores


def test_duplicate_dense_results_do_not_bias_fusion() -> None:
    """A backend that returns an id twice must not double its weight."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"])
    c2 = CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"])
    repo = StubRepo([c1, c2])
    duplicated = StubVectorStore([("c2", 0.9), ("c2", 0.9), ("c1", 0.5)])
    clean = StubVectorStore([("c2", 0.9), ("c1", 0.5)])

    duplicated_scores = dict(
        (chunk.id, score)
        for chunk, score in HybridIndex(repo, duplicated, alpha=0.5).search(  # type: ignore
            "a", [0.0], limit=5
        )
    )
    clean_scores = dict(
        (chunk.id, score)
        for chunk, score in HybridIndex(repo, clean, alpha=0.5).search(  # type: ignore
            "a", [0.0], limit=5
        )
    )

    assert duplicated_scores == clean_scores


def test_a_generation_swap_mid_query_cannot_split_one_search() -> None:
    """Sparse and dense retrieval must observe the same stores.

    ``reload()`` rebinds a service's stores onto a newly published generation.
    A query that read sparse results from one generation and dense results from
    the next would violate the single-generation read invariant.
    """
    old = CodeChunk(id="old", entity_id="e1", content="a", tokens=["a"])
    new = CodeChunk(id="new", entity_id="e2", content="b", tokens=["b"])
    next_generation_repo = StubRepo([new])
    next_generation_vectors = StubVectorStore([("new", 0.9)])

    class SwappingRepo(StubRepo):
        def search_by_tokens(self, tokens, limit=10):  # type: ignore
            # A concurrent reload lands between the sparse and dense reads.
            index.chunk_repo = next_generation_repo
            index.vector_store = next_generation_vectors
            return super().search_by_tokens(tokens, limit=limit)

    repo = SwappingRepo([old])
    index = HybridIndex(repo, StubVectorStore([("old", 0.9)]), alpha=0.5)  # type: ignore
    results = index.search("a", [0.0], limit=5)

    assert [chunk.id for chunk, _ in results] == ["old"]
