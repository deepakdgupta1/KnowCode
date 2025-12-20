"""Reranking for search results."""

from typing import List, Tuple

from knowcode.models import CodeChunk


class Reranker:
    """Reranks search results based on additional signals."""

    def rerank(
        self,
        query: str,
        chunks: list[tuple[CodeChunk, float]],
        boost_recent: bool = True,
        boost_documented: bool = True
    ) -> list[tuple[CodeChunk, float]]:
        """Rerank chunks based on signals.
        
        Args:
            query: Original query
            chunks: (chunk, score) tuples
            boost_recent: Boost recently modified chunks
            boost_documented: Boost chunks with docstrings
            
        Returns:
            Reranked (chunk, score) tuples
        """
        reranked = []
        import time
        current_time = time.time()
        
        for chunk, score in chunks:
            adjusted_score = score
            
            # Boost documented code
            if boost_documented and str(chunk.metadata.get("has_docstring", "")).lower() == "true":
                adjusted_score *= 1.2
            
            # Boost recently modified chunks (within 7 days)
            if boost_recent and chunk.metadata.get("last_modified"):
                try:
                    last_mod = float(chunk.metadata["last_modified"])
                    if current_time - last_mod < 7 * 24 * 3600:
                        adjusted_score *= 1.1
                except (ValueError, TypeError):
                    pass
            
            # Boost exact name matches in content
            if query.lower() in chunk.content.lower():
                adjusted_score *= 1.5
                
            # Boost matches in metadata kind
            if query.lower() == chunk.metadata.get("kind", "").lower():
                adjusted_score *= 2.0
            
            reranked.append((chunk, adjusted_score))
        
        reranked.sort(key=lambda x: x[1], reverse=True)
        return reranked
