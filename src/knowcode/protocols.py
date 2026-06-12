"""Protocol interfaces that decouple service/retrieval components."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    pass


from knowcode.data_models import EmbeddingConfig, Entity


class EmbeddingProviderProtocol(Protocol):
    """Contract for embedding providers used by indexing and retrieval."""

    config: EmbeddingConfig

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""

    def embed_single(self, text: str) -> list[float]:
        """Generate an embedding for a single text."""


class VectorStoreProtocol(Protocol):
    """Contract for dense vector index implementations."""

    dimension: int

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding to the vector index."""

    def search(self, embedding: list[float], limit: int = 10) -> list[tuple[str, float]]:
        """Search for similar chunk IDs and similarity scores."""

    def get_embedding(self, chunk_id: str) -> list[float] | None:
        """Get an embedding by chunk_id."""



    def save(self, path: Path) -> None:
        """Persist vector index artifacts."""

    def load(self, path: Path) -> None:
        """Load vector index artifacts."""

    def clear(self) -> None:
        """Reset in-memory vector index data."""

    def remove(self, chunk_id: str) -> None:
        """Remove a chunk from the index by its ID."""

    def count(self) -> int:
        """Return the number of vectors in the index."""


class KnowledgeStoreProtocol(Protocol):
    """Contract for store access used by retrieval and context synthesis."""

    def get_entity(self, entity_id: str) -> Entity | None:
        """Fetch an entity by ID."""

    def search(self, pattern: str) -> list[Entity]:
        """Search entities by name/qualified name pattern."""

    def get_callers(self, entity_id: str) -> list[Entity]:
        """Return caller entities."""

    def get_callees(self, entity_id: str) -> list[Entity]:
        """Return callee entities."""

    def get_dependencies(self, entity_id: str) -> list[Entity]:
        """Return entities this entity depends on."""

    def get_dependents(self, entity_id: str) -> list[Entity]:
        """Return entities depending on this entity."""
