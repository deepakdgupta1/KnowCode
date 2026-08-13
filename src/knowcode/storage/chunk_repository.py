"""Repository interface for code chunks."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from knowcode.data_models import CodeChunk


@dataclass(frozen=True)
class ChunkFileReplacement:
    """Result of atomically replacing one file's chunk generation.

    ``previous_chunk_ids`` are the IDs that existed for the file before the
    replacement transaction; ``committed_chunk_ids`` are the IDs that are
    searchable after it committed. ``generation_metadata`` carries the
    per-file generation facts (canonical path, counts, and a comparable
    generation stamp) that complete-generation publication (Step 14) extends
    into a cross-artifact generation pointer.
    """

    file_path: str
    previous_chunk_ids: tuple[str, ...]
    committed_chunk_ids: tuple[str, ...]
    generation_metadata: dict[str, Any]


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
    def replace_file(
        self, file_path: Union[str, Path], chunks: list[CodeChunk]
    ) -> ChunkFileReplacement:
        """Atomically replace every chunk for one canonical file identity.

        The replacement runs as a single writer transaction: either all of
        ``chunks`` become searchable for ``file_path`` and the previous chunks
        for that file are removed, or nothing changes. ``file_path`` is
        normalized to the canonical file identity (ADR 1) so callers may pass
        a path alias (symlink, ``..`` segment, unresolved ``/var`` form) and
        still target the rows stored for the resolved source file.

        Returns the previous and committed chunk IDs plus generation metadata.
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
