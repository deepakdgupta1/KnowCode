"""Exact match query engine."""

from knowcode.data_models import CodeChunk
from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.retrieval.search_engine import ScoredChunk


class ExactQueryEngine:
    """Bypasses vector search for exact substring matches."""
    
    def __init__(self, chunk_repo: ChunkRepository) -> None:
        """Initialize with chunk repository."""
        self.chunk_repo = chunk_repo
        
    def search_scored(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = False,
    ) -> list[ScoredChunk]:
        """Execute an exact match search.
        
        Args:
            query: The exact string to search for. If wrapped in quotes, they will be stripped.
            limit: Maximum chunks to return.
            expand_deps: Ignored for exact search, kept for Protocol compatibility.
        """
        if query.startswith('"') and query.endswith('"') and len(query) >= 2:
            query = query[1:-1]
            
        chunks = self.chunk_repo.search_exact(query, limit=limit)
        
        return [
            ScoredChunk(chunk=chunk, score=1.0, source="retrieved")
            for chunk in chunks
        ]
        
    def search(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = False
    ) -> list[CodeChunk]:
        """Execute exact match search and return chunks."""
        scored = self.search_scored(query, limit=limit, expand_deps=expand_deps)
        return [s.chunk for s in scored]
