"""VoyageAI client for embeddings and reranking.

Provides integration with VoyageAI's embedding and reranking APIs:
- voyage-3-lite: Fast embeddings (1024 dimensions)
- rerank-2.5: Cross-encoder reranking for improved relevance
"""

import os
from typing import Any, Optional

# VoyageAI imports - requires: pip install voyageai
try:
    import voyageai
    VOYAGEAI_AVAILABLE = True
except ImportError:
    VOYAGEAI_AVAILABLE = False


class VoyageAIClient:
    """Client for VoyageAI embedding and reranking APIs."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_key_env: str = "VOYAGE_API_KEY_1",
    ) -> None:
        """Initialize VoyageAI client.
        
        Args:
            api_key: API key (if None, reads from api_key_env).
            api_key_env: Environment variable name for API key.
        """
        if not VOYAGEAI_AVAILABLE:
            raise ImportError(
                "VoyageAI package not installed. Install with: pip install voyageai"
            )
        
        self.api_key = api_key or os.environ.get(api_key_env)
        if not self.api_key:
            raise ValueError(
                f"VoyageAI API key not found. Set {api_key_env} or pass api_key."
            )
        
        self.client = voyageai.Client(api_key=self.api_key)
    
    def embed(
        self,
        texts: list[str],
        model: str = "voyage-3-lite",
        input_type: str = "document",
    ) -> list[list[float]]:
        """Generate embeddings for texts.
        
        Args:
            texts: List of texts to embed.
            model: Model name (voyage-3-lite, voyage-3, voyage-code-3).
            input_type: "query" or "document".
            
        Returns:
            List of embedding vectors.
        """
        result = self.client.embed(
            texts=texts,
            model=model,
            input_type=input_type,
        )
        return result.embeddings
    
    def rerank(
        self,
        query: str,
        documents: list[str],
        model: str = "rerank-2.5",
        top_k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Rerank documents using cross-encoder.
        
        Args:
            query: Search query.
            documents: List of document texts to rerank.
            model: Reranking model (rerank-2.5).
            top_k: Return only top K results (None = all).
            
        Returns:
            List of dicts with 'index', 'relevance_score', 'document'.
        """
        if not documents:
            return []
        
        result = self.client.rerank(
            query=query,
            documents=documents,
            model=model,
            top_k=top_k or len(documents),
        )
        
        return [
            {
                "index": r.index,
                "relevance_score": r.relevance_score,
                "document": documents[r.index],
            }
            for r in result.results
        ]


def get_voyageai_client(api_key_env: str = "VOYAGE_API_KEY_1") -> Optional[VoyageAIClient]:
    """Get VoyageAI client if available and configured.
    
    Args:
        api_key_env: Environment variable for API key.
        
    Returns:
        VoyageAIClient or None if not available.
    """
    try:
        return VoyageAIClient(api_key_env=api_key_env)
    except (ImportError, ValueError):
        return None
