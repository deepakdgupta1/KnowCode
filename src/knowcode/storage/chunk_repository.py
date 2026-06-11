"""Repository interface for code chunks."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from knowcode.data_models import CodeChunk


class ChunkRepository(ABC):
    """Abstract interface for chunk storage and retrieval."""

    @abstractmethod
    def add(self, chunk: CodeChunk) -> None:
        """Add a chunk to the repository."""
        pass

    def add_batch(self, chunks: list[CodeChunk]) -> None:
        """Add multiple chunks in a single operation.

        The default implementation loops over ``add()``.  Subclasses may
        override with a more efficient bulk-insert strategy.
        """
        for chunk in chunks:
            self.add(chunk)

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
    def remove_by_file(self, file_path: str) -> list[str]:
        """Remove all chunks associated with the given file path.
        
        Returns:
            List of removed chunk IDs.
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all chunks."""
        pass

    def count(self) -> int:
        """Return the number of stored chunks."""
        return 0

    def close(self) -> None:
        """Release any underlying resources.

        The default is a no-op.  Subclasses with file handles or database
        connections should override.
        """
        pass

    @abstractmethod
    def save(self, path: Path) -> None:
        """Persist chunk repository data to disk (if applicable)."""
        pass

    @abstractmethod
    def load(self, path: Path) -> None:
        """Load chunk repository data from disk (if applicable)."""
        pass

    @abstractmethod
    def get_all(self) -> list[CodeChunk]:
        """Get all chunks in the repository."""
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

    def remove_by_file(self, file_path: str) -> list[str]:
        """Remove all chunks associated with the given file path."""
        removed_ids = []
        for entity_id in list(self._by_entity.keys()):
            if entity_id == file_path or entity_id.startswith(file_path + "::"):
                chunk_ids = self._by_entity.pop(entity_id, [])
                for cid in chunk_ids:
                    if cid in self._chunks:
                        self._chunks.pop(cid, None)
                        removed_ids.append(cid)
        return removed_ids

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

    def count(self) -> int:
        """Return the number of stored chunks."""
        return len(self._chunks)

    def save(self, path: Path) -> None:
        """Save chunk metadata to chunks.json."""
        import json
        metadata = {
            "schema_version": 2,
            "chunks": [
                {
                    "id": c.id,
                    "entity_id": c.entity_id,
                    "content": c.content,
                    "tokens": c.tokens,
                    "metadata": c.metadata,
                }
                for c in self._chunks.values()
            ],
        }
        with open(path / "chunks.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f)

    def load(self, path: Path) -> None:
        """Load chunk metadata from chunks.json."""
        import json
        chunks_file = path / "chunks.json"
        if not chunks_file.exists():
            return
            
        with open(chunks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        if not isinstance(data, dict):
            return

        # Perform schema validation / migration
        schema_version = data.get("schema_version")
        if schema_version is not None:
            # Normalize version
            norm = None
            if isinstance(schema_version, int) and not isinstance(schema_version, bool):
                norm = schema_version
            elif isinstance(schema_version, str) and schema_version.isdigit():
                norm = int(schema_version)
            else:
                raise ValueError(f"Invalid chunks metadata schema version value: {schema_version!r}")
                
            if norm not in (1, 2):
                raise ValueError(
                    f"Unsupported chunks metadata schema version {schema_version!r}. "
                    "Supported versions: [2]. Rebuild with `knowcode build`."
                )
            
        chunk_entries = data.get("chunks", [])
        if isinstance(chunk_entries, list):
            for c_data in chunk_entries:
                if not isinstance(c_data, dict):
                    continue
                chunk = CodeChunk(
                    id=c_data.get("id", ""),
                    entity_id=c_data.get("entity_id", ""),
                    content=c_data.get("content", ""),
                    tokens=c_data.get("tokens", []),
                    metadata=c_data.get("metadata", {}),
                )
                self.add(chunk)

    def get_all(self) -> list[CodeChunk]:
        """Return all chunks in the repository."""
        return list(self._chunks.values())

