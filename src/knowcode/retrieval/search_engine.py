"""Search engine orchestrating the retrieval pipeline."""

from typing import Optional

from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.retrieval.completeness import expand_dependencies
from knowcode.llm.embedding import EmbeddingProvider
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.models import CodeChunk
from knowcode.retrieval.reranker import Reranker


class SearchEngine:
    """Orchestrates: embed query -> hybrid search -> rerank -> completeness."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        embedding_provider: EmbeddingProvider,
        hybrid_index: HybridIndex,
        knowledge_store: KnowledgeStore,
    ) -> None:
        """Initialize the search engine pipeline.

        Args:
            chunk_repo: Chunk repository backing BM25 search.
            embedding_provider: Provider for generating query embeddings.
            hybrid_index: Combined sparse+dense index used for retrieval.
            knowledge_store: Graph store used to expand dependency context.
        """
        self.chunk_repo = chunk_repo
        self.embedding_provider = embedding_provider
        self.hybrid_index = hybrid_index
        self.knowledge_store = knowledge_store
        self.reranker = Reranker()

    def search(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = True
    ) -> list[CodeChunk]:
        """Execute the full search pipeline.

        Args:
            query: Natural language query string.
            limit: Maximum number of chunks to return.
            expand_deps: Whether to include dependency context from the graph.

        Returns:
            Ranked list of CodeChunk objects.
        """
        # 1. Embed query
        query_embedding = self.embedding_provider.embed_single(query)
        
        # 2. Hybrid search
        results = self.hybrid_index.search(query, query_embedding, limit=limit * 2)
        
        # 3. Rerank
        reranked = self.reranker.rerank(query, results)
        
        # 4. Extract top chunks
        top_chunks = [chunk for chunk, _ in reranked[:limit]]
        
        # 5. Expand dependencies if requested
        if expand_deps:
            final_chunks = self._expand_dependencies(top_chunks)
            # Re-limit after expansion if needed, or just return all
            return final_chunks
        
        return top_chunks

    def _expand_dependencies(self, chunks: list[CodeChunk]) -> list[CodeChunk]:
        """Add dependency context to the results using the knowledge graph."""
        expanded = []
        seen_ids = set()
        
        for chunk in chunks:
            # Expand using graph
            deps = expand_dependencies(
                chunk,
                self.chunk_repo,
                self.knowledge_store,
                max_depth=1
            )
            for d in deps:
                if d.id not in seen_ids:
                    expanded.append(d)
                    seen_ids.add(d.id)
                    
        return expanded
