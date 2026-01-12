"""Search engine orchestrating the retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.retrieval.completeness import expand_dependencies
from knowcode.llm.embedding import EmbeddingProvider
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.data_models import CodeChunk
from knowcode.retrieval.reranker import Reranker
from knowcode.config import AppConfig


@dataclass(frozen=True)
class ScoredChunk:
    """A retrieved chunk with an attached score and provenance."""

    chunk: CodeChunk
    score: float
    source: str  # "retrieved" | "dependency"


class SearchEngine:
    """Orchestrates: embed query -> hybrid search -> rerank -> completeness."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        embedding_provider: EmbeddingProvider,
        hybrid_index: HybridIndex,
        knowledge_store: KnowledgeStore,
        config: Optional[AppConfig] = None,
        use_voyageai_reranking: bool = True,
    ) -> None:
        """Initialize the search engine pipeline.

        Args:
            chunk_repo: Chunk repository backing BM25 search.
            embedding_provider: Provider for generating query embeddings.
            hybrid_index: Combined sparse+dense index used for retrieval.
            knowledge_store: Graph store used to expand dependency context.
            config: AppConfig with reranking model settings.
            use_voyageai_reranking: Whether to try VoyageAI for reranking.
        """
        self.chunk_repo = chunk_repo
        self.embedding_provider = embedding_provider
        self.hybrid_index = hybrid_index
        self.knowledge_store = knowledge_store
        self.reranker = Reranker(
            use_voyageai=use_voyageai_reranking,
            config=config,
        )

    def search_scored(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = True,
    ) -> list[ScoredChunk]:
        """Execute the full search pipeline and return scored chunks.

        This is similar to search(), but retains relevance scores and labels
        dependency-expanded chunks so callers can build evidence-aware outputs.

        Args:
            query: Natural language query string.
            limit: Maximum number of primary chunks to return (before expansion).
            expand_deps: Whether to include dependency context from the graph.

        Returns:
            Ranked list of ScoredChunk objects.
        """
        query_embedding = self.embedding_provider.embed_single(query)
        results = self.hybrid_index.search(query, query_embedding, limit=limit * 2)
        reranked = self.reranker.rerank(query, results, top_k=limit)
        primary = [ScoredChunk(chunk=c, score=s, source="retrieved") for c, s in reranked]

        if not expand_deps:
            return primary

        expanded: list[ScoredChunk] = []
        seen_ids: set[str] = set()

        for scored in primary:
            deps = expand_dependencies(
                scored.chunk,
                self.chunk_repo,
                self.knowledge_store,
                max_depth=1,
            )
            for dep in deps:
                if dep.id in seen_ids:
                    continue
                seen_ids.add(dep.id)
                expanded.append(
                    ScoredChunk(
                        chunk=dep,
                        score=scored.score if dep.id == scored.chunk.id else 0.0,
                        source="retrieved" if dep.id == scored.chunk.id else "dependency",
                    )
                )

        return expanded

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
        scored = self.search_scored(query, limit=limit, expand_deps=expand_deps)
        return [s.chunk for s in scored]

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
