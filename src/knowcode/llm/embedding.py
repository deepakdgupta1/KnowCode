"""Embedding providers for semantic search."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os

from openai import OpenAI

from knowcode.config import AppConfig
from knowcode.data_models import EmbeddingConfig

_OPENAI_EMBED_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

_VOYAGE_EMBED_DIMENSIONS: dict[str, int] = {
    "voyage-3-lite": 1024,
    "voyage-3": 1024,
    "voyage-code-3": 1024,
}


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

    def __init__(
        self,
        config: EmbeddingConfig,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
    ) -> None:
        """Create an OpenAI-backed embedding provider.

        Args:
            config: Embedding configuration settings.
            api_key_env: Environment variable containing the API key.
            base_url: Optional base URL for OpenAI-compatible providers.
        """
        super().__init__(config)
        self.api_key_env = api_key_env
        self.base_url = base_url

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            # We allow init without key, but embed() will fail if not provided later
            self.client = None
        else:
            self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _get_client(self) -> OpenAI:
        """Return an initialized OpenAI client, loading credentials if needed."""
        if not self.client:
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise ValueError(f"{self.api_key_env} environment variable is not set.")
            self.client = OpenAI(api_key=api_key, base_url=self.base_url)
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


class VoyageAIEmbeddingProvider(EmbeddingProvider):
    """VoyageAI embedding provider."""

    def __init__(
        self,
        config: EmbeddingConfig,
        api_key_env: str = "VOYAGE_API_KEY_1",
    ) -> None:
        """Create a VoyageAI-backed embedding provider.

        Args:
            config: Embedding configuration settings.
            api_key_env: Environment variable containing the VoyageAI API key.
        """
        super().__init__(config)
        self.api_key_env = api_key_env
        self.client = None

    def _get_client(self):
        """Return an initialized VoyageAI client, loading credentials if needed."""
        if self.client is None:
            from knowcode.llm.voyageai_client import get_voyageai_client

            self.client = get_voyageai_client(self.api_key_env)

        if self.client is None:
            raise ValueError(
                f"VoyageAI client unavailable; set {self.api_key_env} and install "
                "optional dependency with: pip install \"knowcode[voyageai]\""
            )

        return self.client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate document embeddings for a batch of texts."""
        if not texts:
            return []

        client = self._get_client()
        embeddings = client.embed(
            texts=texts,
            model=self.config.model_name,
            input_type="document",
        )
        if not embeddings:
            return []

        if self.config.normalize:
            embeddings = [self._normalize(e) for e in embeddings]

        return embeddings

    def embed_single(self, text: str) -> list[float]:
        """Generate a query embedding for a single text input."""
        client = self._get_client()
        embeddings = client.embed(
            texts=[text],
            model=self.config.model_name,
            input_type="query",
        )
        if not embeddings:
            return []

        emb = embeddings[0]
        return self._normalize(emb) if self.config.normalize else emb

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        import math

        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec


def create_embedding_provider(
    app_config: AppConfig | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> EmbeddingProvider:
    """Create an embedding provider from explicit config or AppConfig.

    Selection precedence:
    1) Explicit EmbeddingConfig (embedding_config.provider)
    2) First usable model in app_config.embedding_models
    3) Default OpenAIEmbeddingProvider with default EmbeddingConfig
    """
    if embedding_config is not None:
        provider = embedding_config.provider.lower()
        if provider in {"voyageai", "voyage"}:
            return VoyageAIEmbeddingProvider(embedding_config)
        return OpenAIEmbeddingProvider(embedding_config)

    if app_config and app_config.embedding_models:
        for model in app_config.embedding_models:
            api_key = os.environ.get(model.api_key_env)
            if not api_key:
                continue

            provider = model.provider.lower()
            if provider in {"voyageai", "voyage"}:
                cfg = EmbeddingConfig(
                    provider="voyageai",
                    model_name=model.name,
                    dimension=_VOYAGE_EMBED_DIMENSIONS.get(model.name, 1024),
                )
                return VoyageAIEmbeddingProvider(cfg, api_key_env=model.api_key_env)

            if provider in {"openai", "openrouter", "mistralai"}:
                base_url = None
                if provider in {"openrouter", "mistralai"}:
                    base_url = "https://openrouter.ai/api/v1"

                cfg = EmbeddingConfig(
                    provider="openai",
                    model_name=model.name,
                    dimension=_OPENAI_EMBED_DIMENSIONS.get(model.name, 1536),
                )
                return OpenAIEmbeddingProvider(
                    cfg,
                    api_key_env=model.api_key_env,
                    base_url=base_url,
                )

    return OpenAIEmbeddingProvider(EmbeddingConfig())
