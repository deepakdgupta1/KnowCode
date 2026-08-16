"""Hybrid BM25 + Vector search index."""

from typing import Iterable

from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.data_models import CodeChunk
from knowcode.protocols import VectorStoreProtocol
from knowcode.utils.tokenizer import tokenize_code

#: Reciprocal-rank-fusion constant: avoids division by zero and dampens the
#: weight difference between adjacent top ranks.
RRF_K = 60

#: How many candidates each retriever contributes per requested result.
CANDIDATE_FACTOR = 3


class HybridIndex:
    """Combines BM25 sparse retrieval with dense vector search."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        vector_store: VectorStoreProtocol,
        alpha: float = 0.2,  # Dense weight; 0.2 = sparse-heavy, matches AppConfig default
    ) -> None:
        """Initialize the hybrid index.

        Args:
            chunk_repo: Repository providing BM25-style token search.
            vector_store: Dense vector store for semantic similarity.
            alpha: Blend weight for dense vs sparse results.
        """
        self.chunk_repo = chunk_repo
        self.vector_store = vector_store
        self.alpha = alpha

    def search(
        self, query: str, query_embedding: list[float], limit: int = 10
    ) -> list[tuple[CodeChunk, float]]:
        """Search using hybrid retrieval.

        Combines BM25 sparse retrieval with dense vector search and fuses the
        two rankings with Reciprocal Rank Fusion.

        Sparse and dense retrieval read **one snapshot** of the stores: the
        repository and vector store are bound once for the whole query, so a
        generation swap between the two reads cannot answer half a query from
        each generation (Step 15). Ids from either retriever are de-duplicated
        before fusion, because a repeated id would otherwise score twice and
        outrank better matches. A dense id the repository cannot resolve —
        the signature of a chunk/vector split — is skipped without consuming
        one of the ``limit`` result slots.

        Args:
            query: Raw query string for sparse matching.
            query_embedding: Dense embedding of the query.
            limit: Maximum number of chunks to return.

        Returns:
            List of (chunk, score) tuples ranked by reciprocal rank fusion.
        """
        # Bind the snapshot first: everything below reads these two, never the
        # attributes, which a concurrent reload may rebind.
        chunk_repo = self.chunk_repo
        vector_store = self.vector_store
        candidates = max(limit, 0) * CANDIDATE_FACTOR

        query_tokens = tokenize_code(query)
        sparse_results = chunk_repo.search_by_tokens(query_tokens, limit=candidates)
        dense_results = vector_store.search(query_embedding, limit=candidates)

        combined_scores: dict[str, float] = {}
        for rank, chunk_id in enumerate(self._unique_ids(c.id for c in sparse_results)):
            combined_scores[chunk_id] = combined_scores.get(chunk_id, 0.0) + (
                (1.0 - self.alpha) / (RRF_K + rank + 1)
            )
        for rank, chunk_id in enumerate(
            self._unique_ids(chunk_id for chunk_id, _ in dense_results)
        ):
            combined_scores[chunk_id] = combined_scores.get(chunk_id, 0.0) + (
                self.alpha / (RRF_K + rank + 1)
            )

        ranked = sorted(combined_scores.items(), key=lambda item: item[1], reverse=True)

        results: list[tuple[CodeChunk, float]] = []
        for chunk_id, score in ranked:
            if len(results) >= limit:
                break
            retrieved_chunk = chunk_repo.get(chunk_id)
            if retrieved_chunk is not None:
                results.append((retrieved_chunk, score))

        return results

    @staticmethod
    def _unique_ids(chunk_ids: Iterable[str]) -> list[str]:
        """Return ids in rank order, keeping only each id's best rank."""
        seen: set[str] = set()
        ordered: list[str] = []
        for chunk_id in chunk_ids:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            ordered.append(chunk_id)
        return ordered
