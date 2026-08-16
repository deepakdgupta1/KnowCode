"""Embedding providers for semantic search."""

from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import math
import os
from typing import Any, cast

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import EmbeddingConfig

_OPENAI_EMBED_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

_VOYAGE_EMBED_DIMENSIONS: dict[str, int] = {
    "voyage-3-lite": 1024,
    "voyage-3": 1024,
    "voyage-3-large": 1024,
    "voyage-code-3": 1024,
}


def _create_openai_client(api_key: str, base_url: str | None) -> Any:
    """Create an OpenAI client with an actionable dependency hint."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on environment extras
        raise ImportError("Install knowcode[llm] to use 'knowcode index'.") from exc
    return OpenAI(api_key=api_key, base_url=base_url)


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
            self.client = _create_openai_client(api_key=api_key, base_url=base_url)

    def _get_client(self) -> Any:
        """Return an initialized OpenAI client, loading credentials if needed."""
        if not self.client:
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise ValueError(f"{self.api_key_env} environment variable is not set.")
            self.client = _create_openai_client(api_key=api_key, base_url=self.base_url)
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
        response = client.embeddings.create(model=self.config.model_name, input=texts)
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

        norm = math.sqrt(sum(x * x for x in vec))
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
        self.client: Any = None

    def _get_client(self) -> Any:
        """Return an initialized VoyageAI client, loading credentials if needed."""
        if self.client is None:
            from knowcode.llm.voyageai_client import get_voyageai_client

            self.client = get_voyageai_client(self.api_key_env)

        if self.client is None:
            raise ValueError(
                f"VoyageAI client unavailable; set {self.api_key_env} and install "
                'optional dependency with: pip install "knowcode[voyageai]"'
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

        return cast(list[list[float]], embeddings)

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


class DummyEmbeddingProvider(EmbeddingProvider):
    """Deterministic embedding fallback that requires no external API keys."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate stable pseudo-embeddings for a batch of texts."""
        return [self.embed_single(text) for text in texts]

    def embed_single(self, text: str) -> list[float]:
        """Generate a stable pseudo-embedding for one text."""
        dimension = max(0, self.config.dimension)
        if dimension == 0:
            return []

        values: list[float] = []
        seed = text.encode("utf-8", errors="replace")
        counter = 0
        while len(values) < dimension:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            values.extend((byte / 127.5) - 1.0 for byte in digest)
            counter += 1

        vector = values[:dimension]
        return self._normalize(vector) if self.config.normalize else vector

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec


def resolve_embedding_dimension(provider: str, model_name: str) -> int:
    """Resolve the vector dimension for a configured embedding model."""
    normalized_provider = provider.lower()
    if normalized_provider in {"voyageai", "voyage"}:
        return _VOYAGE_EMBED_DIMENSIONS.get(model_name, 1024)
    if normalized_provider in {"openai", "openrouter", "mistralai"}:
        return _OPENAI_EMBED_DIMENSIONS.get(model_name, 1536)
    raise ValueError(f"Unsupported embedding provider: {provider}")


def build_provider_from_model(model: ModelConfig) -> EmbeddingProvider:
    """Build an embedding provider from one model configuration."""
    provider = model.provider.lower()

    if provider in {"voyageai", "voyage"}:
        config = EmbeddingConfig(
            provider="voyageai",
            model_name=model.name,
            dimension=resolve_embedding_dimension(provider, model.name),
        )
        return VoyageAIEmbeddingProvider(config, api_key_env=model.api_key_env)

    if provider in {"openai", "openrouter", "mistralai"}:
        base_url = (
            "https://openrouter.ai/api/v1"
            if provider in {"openrouter", "mistralai"}
            else None
        )
        config = EmbeddingConfig(
            provider="openai",
            model_name=model.name,
            dimension=resolve_embedding_dimension(provider, model.name),
        )
        return OpenAIEmbeddingProvider(
            config,
            api_key_env=model.api_key_env,
            base_url=base_url,
        )

    if provider == "local":
        raise NotImplementedError("The local embedding provider is not implemented.")

    raise ValueError(f"Unsupported embedding provider: {model.provider}")


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

            return build_provider_from_model(model)

    return DummyEmbeddingProvider(EmbeddingConfig())


def create_prose_embedding_provider(
    app_config: AppConfig | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> EmbeddingProvider:
    """Create an embedding provider for SDLC prose documentation collateral.

    Selection precedence:
    1) Explicit EmbeddingConfig (embedding_config.provider).
    2) The first usable model in app_config.prose_embedding_models (one whose
       API key is present in the environment).
    3) Fall back to the code embedding provider (create_embedding_provider),
       so prose retrieval degrades to the code embedder when no dedicated prose
       model is usable.
    4) DummyEmbeddingProvider when no AppConfig is given at all.

    Args:
        app_config: Optional application configuration with prose/code model lists.
        embedding_config: Optional explicit embedding configuration.

    Returns:
        A concrete EmbeddingProvider for prose chunks.
    """
    if embedding_config is not None:
        provider = embedding_config.provider.lower()
        if provider in {"voyageai", "voyage"}:
            return VoyageAIEmbeddingProvider(embedding_config)
        return OpenAIEmbeddingProvider(embedding_config)

    if app_config and app_config.prose_embedding_models:
        for model in app_config.prose_embedding_models:
            if not os.environ.get(model.api_key_env):
                continue
            return build_provider_from_model(model)

    # No usable prose model — fall back to the code embedder, then the dummy.
    if app_config and (app_config.embedding_models or app_config.models):
        code_provider = create_embedding_provider(app_config=app_config)
        # create_embedding_provider already returns DummyEmbeddingProvider when
        # nothing is usable, so no further fallback is required here.
        return code_provider

    return DummyEmbeddingProvider(EmbeddingConfig())
