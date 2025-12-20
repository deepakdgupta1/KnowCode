"""Unit tests for embedding providers."""

from knowcode.llm.embedding import OpenAIEmbeddingProvider
from knowcode.models import EmbeddingConfig


def test_embedding_provider_empty_batch() -> None:
    """Embedding provider should return empty list for empty input."""
    provider = OpenAIEmbeddingProvider(EmbeddingConfig())
    assert provider.embed([]) == []


def test_embedding_provider_normalize_zero_vector() -> None:
    """Normalization should handle zero vectors safely."""
    provider = OpenAIEmbeddingProvider(EmbeddingConfig())
    assert provider._normalize([0.0, 0.0]) == [0.0, 0.0]
