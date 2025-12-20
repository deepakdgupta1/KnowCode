"""Repository interface for code chunks."""

from abc import ABC, abstractmethod
from typing import Optional

from knowcode.data_models import CodeChunk


class ChunkRepository(ABC):
    """Abstract interface for chunk storage and retrieval."""

    @abstractmethod
    def add(self, chunk: CodeChunk) -> None:
        """Add a chunk to the repository."""
        pass

    @abstractmethod
    def get(self, chunk_id: str) -> Optional[CodeChunk]:
        """Get a chunk by ID."""
        pass

    @abstractmethod
    def get_by_entity(self, entity_id: str) -> list[CodeChunk]:
        """Get all chunks for an entity."""
        pass

    @abstractmethod
    def search_by_tokens(self, tokens: list[str], limit: int = 10) -> list[CodeChunk]:
        """Search chunks by BM25 tokens."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all chunks."""
        pass


class InMemoryChunkRepository(ChunkRepository):
    """In-memory implementation of ChunkRepository."""

    def __init__(self) -> None:
        """Initialize the in-memory storage structures."""
        self._chunks: dict[str, CodeChunk] = {}
        self._by_entity: dict[str, list[str]] = {}  # entity_id -> chunk_ids

    def add(self, chunk: CodeChunk) -> None:
        """Add a chunk to the in-memory index."""
        self._chunks[chunk.id] = chunk
        if chunk.entity_id not in self._by_entity:
            self._by_entity[chunk.entity_id] = []
        if chunk.id not in self._by_entity[chunk.entity_id]:
            self._by_entity[chunk.entity_id].append(chunk.id)

    def get(self, chunk_id: str) -> Optional[CodeChunk]:
        """Fetch a chunk by its ID."""
        return self._chunks.get(chunk_id)

    def get_by_entity(self, entity_id: str) -> list[CodeChunk]:
        """Return all chunks associated with an entity."""
        chunk_ids = self._by_entity.get(entity_id, [])
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    def search_by_tokens(self, tokens: list[str], limit: int = 10) -> list[CodeChunk]:
        """Perform a simple token-overlap search over stored chunks."""
        # Simple token overlap scoring
        scores: list[tuple[float, CodeChunk]] = []
        query_set = set(tokens)
        for chunk in self._chunks.values():
            if chunk.tokens:
                overlap = len(query_set & set(chunk.tokens))
                if overlap > 0:
                    scores.append((float(overlap), chunk))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scores[:limit]]

    def clear(self) -> None:
        self._chunks.clear()
        self._by_entity.clear()
