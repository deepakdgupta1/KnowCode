"""Embedding providers for semantic search."""

from abc import ABC, abstractmethod
import os
from typing import Optional

from openai import OpenAI
from knowcode.models import EmbeddingConfig


class EmbeddingProvider(ABC):
    """Abstract interface for generating embeddings."""

    def __init__(self, config: EmbeddingConfig) -> None:
        """Initialize the provider with the embedding configuration."""
        self.config = config

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        pass

    @abstractmethod
    def embed_single(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        pass


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embedding provider."""

    def __init__(self, config: EmbeddingConfig) -> None:
        """Create an OpenAI-backed embedding provider.

        Args:
            config: Embedding configuration settings.
        """
        super().__init__(config)
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            # We allow init without key, but embed() will fail if not provided later
            self.client = None
        else:
            self.client = OpenAI(api_key=api_key)

    def _get_client(self) -> OpenAI:
        """Return an initialized OpenAI client, loading credentials if needed."""
        if not self.client:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY environment variable is not set.")
            self.client = OpenAI(api_key=api_key)
        return self.client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: Input texts to embed.

        Returns:
            List of embedding vectors (one per input).
        """
        if not texts:
            return []
            
        client = self._get_client()
        response = client.embeddings.create(
            model=self.config.model_name,
            input=texts
        )
        embeddings = [item.embedding for item in response.data]
        
        if self.config.normalize:
            embeddings = [self._normalize(e) for e in embeddings]
            
        return embeddings

    def embed_single(self, text: str) -> list[float]:
        """Generate an embedding for a single text input."""
        return self.embed([text])[0]

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        import math
        norm = math.sqrt(sum(x*x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec
