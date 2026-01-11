"""Reranking for search results.

Supports two reranking strategies:
1. VoyageAI cross-encoder (rerank-2.5) - high quality, requires API key
2. Signal-based scoring - local, no API required
"""

import os
import time
from typing import Optional

from knowcode.data_models import CodeChunk
from knowcode.config import AppConfig


class Reranker:
    """Reranks search results using cross-encoder or signal-based scoring."""
    
    def __init__(
        self,
        use_voyageai: bool = True,
        api_key_env: Optional[str] = None,
        config: Optional[AppConfig] = None,
    ) -> None:
        """Initialize reranker.
        
        Args:
            use_voyageai: Try to use VoyageAI if available.
            api_key_env: Environment variable for VoyageAI API key.
            config: AppConfig with reranking model settings.
        """
        self.voyage_client = None
        self.model = "rerank-2.5"
        
        if use_voyageai:
            # Determine API key env from config or default
            if config and config.reranking_models:
                api_key_env = config.reranking_models[0].api_key_env
                self.model = config.reranking_models[0].name
            else:
                api_key_env = api_key_env or "VOYAGE_API_KEY_1"
            
            # Try to initialize VoyageAI client
            try:
                from knowcode.llm.voyageai_client import get_voyageai_client
                self.voyage_client = get_voyageai_client(api_key_env)
            except Exception:
                pass  # Fall back to signal-based
    
    def rerank(
        self,
        query: str,
        chunks: list[tuple[CodeChunk, float]],
        boost_recent: bool = True,
        boost_documented: bool = True,
        top_k: Optional[int] = None,
    ) -> list[tuple[CodeChunk, float]]:
        """Rerank chunks based on semantic relevance.
        
        Uses VoyageAI cross-encoder if available, otherwise falls back
        to signal-based scoring.
        
        Args:
            query: Original search query.
            chunks: (chunk, score) tuples from initial retrieval.
            boost_recent: Boost recently modified chunks.
            boost_documented: Boost chunks with docstrings.
            top_k: Return only top K results.
            
        Returns:
            Reranked (chunk, score) tuples.
        """
        if not chunks:
            return []
        
        # Try VoyageAI cross-encoder reranking
        if self.voyage_client:
            try:
                return self._rerank_with_voyageai(query, chunks, top_k)
            except Exception as e:
                print(f"  ⚠️ VoyageAI reranking failed: {e}. Using signal-based fallback.")
        
        # Fallback to signal-based reranking
        return self._rerank_with_signals(
            query, chunks, boost_recent, boost_documented, top_k
        )
    
    def _rerank_with_voyageai(
        self,
        query: str,
        chunks: list[tuple[CodeChunk, float]],
        top_k: Optional[int] = None,
    ) -> list[tuple[CodeChunk, float]]:
        """Rerank using VoyageAI cross-encoder.
        
        Args:
            query: Search query.
            chunks: (chunk, score) tuples.
            top_k: Maximum results.
            
        Returns:
            Reranked chunks with cross-encoder scores.
        """
        # Prepare documents for reranking
        documents = [chunk.content for chunk, _ in chunks]
        
        # Call VoyageAI rerank API
        results = self.voyage_client.rerank(
            query=query,
            documents=documents,
            model=self.model,
            top_k=top_k,
        )
        
        # Map back to chunks with new scores
        reranked = []
        for r in results:
            idx = r["index"]
            chunk, _ = chunks[idx]
            reranked.append((chunk, r["relevance_score"]))
        
        return reranked
    
    def _rerank_with_signals(
        self,
        query: str,
        chunks: list[tuple[CodeChunk, float]],
        boost_recent: bool = True,
        boost_documented: bool = True,
        top_k: Optional[int] = None,
    ) -> list[tuple[CodeChunk, float]]:
        """Rerank using local heuristic signals.
        
        Args:
            query: Search query.
            chunks: (chunk, score) tuples.
            boost_recent: Boost recent modifications.
            boost_documented: Boost documented code.
            top_k: Maximum results.
            
        Returns:
            Reranked chunks with adjusted scores.
        """
        reranked = []
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
        
        if top_k:
            reranked = reranked[:top_k]
            
        return reranked

