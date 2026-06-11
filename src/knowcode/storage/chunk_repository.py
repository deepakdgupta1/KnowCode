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
    def search_exact(self, pattern: str, limit: int = 10) -> list[CodeChunk]:
        """Search chunks by exact substring match."""
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

    @abstractmethod
    def get_chunk_id_by_hash(self, content_hash: str) -> Optional[str]:
        """Find a chunk_id given a content_hash (from metadata)."""
        pass

    @abstractmethod
    def get_all_file_paths(self) -> set[str]:
        """Return a set of all file paths currently in the repository."""
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




