"""Hybrid BM25 + Vector search index."""

from typing import Optional

from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.data_models import CodeChunk
from knowcode.utils.tokenizer import tokenize_code
from knowcode.storage.vector_store import VectorStore


class HybridIndex:
    """Combines BM25 sparse retrieval with dense vector search."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        vector_store: VectorStore,
        alpha: float = 0.5  # Weight for dense vs sparse (0.5 = equal weight)
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
        self,
        query: str,
        query_embedding: list[float],
        limit: int = 10
    ) -> list[tuple[CodeChunk, float]]:
        """Search using hybrid retrieval. 
        Combines BM25 sparse retrieval with dense vector search.         
        Returns chunks with combined scores using Reciprocal Rank Fusion (RRF).

        Args:
            query: Raw query string for sparse matching.
            query_embedding: Dense embedding of the query.
            limit: Maximum number of chunks to return.

        Returns:
            List of (chunk, score) tuples ranked by reciprocal rank fusion.
        """
        # 1. BM25 Search
        query_tokens = tokenize_code(query)
        # We get more results for fusion
        sparse_results = self.chunk_repo.search_by_tokens(query_tokens, limit=limit * 2)
        
        # 2. Vector Search
        dense_results = self.vector_store.search(query_embedding, limit=limit * 2)
        
        # 3. Combine scores (RRF)
        combined_scores: dict[str, float] = {}
        
        # Constant for RRF to avoid division by zero and dampen top ranks
        K = 60
        
        for rank, chunk in enumerate(sparse_results):
            combined_scores[chunk.id] = combined_scores.get(chunk.id, 0.0) + (1.0 - self.alpha) / (K + rank + 1)
            
        for rank, (chunk_id, _) in enumerate(dense_results):
            combined_scores[chunk_id] = combined_scores.get(chunk_id, 0.0) + self.alpha / (K + rank + 1)
            
        # 4. Sort and return
        sorted_ids = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
        
        results = []
        for chunk_id, score in sorted_ids[:limit]:
            chunk = self.chunk_repo.get(chunk_id)
            if chunk:
                results.append((chunk, score))
                
        return results
