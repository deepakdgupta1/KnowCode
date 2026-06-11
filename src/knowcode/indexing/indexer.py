"""Indexing pipeline for code chunks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from knowcode.storage.chunk_repository import ChunkRepository, InMemoryChunkRepository
from knowcode.indexing.chunker import Chunker
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.scanner import Scanner
from knowcode.protocols import EmbeddingProviderProtocol, VectorStoreProtocol
from knowcode.storage.vector_store import VectorStore
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


class Indexer:
    """Orchestrates scan -> chunk -> embed -> index pipeline."""

    SCHEMA_VERSION = 2
    LEGACY_MANIFEST_VERSION = 1
    SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}

    def __init__(
        self,
        embedding_provider: EmbeddingProviderProtocol,
        chunk_repo: Optional[ChunkRepository] = None,
        vector_store: Optional[VectorStoreProtocol] = None,
    ) -> None:
        """Initialize an indexer with optional storage backends.

        Args:
            embedding_provider: Provider used to generate chunk embeddings.
            chunk_repo: Optional chunk repository (defaults to in-memory).
            vector_store: Optional vector store (defaults to FAISS-backed store).
        """
        self.embedding_provider = embedding_provider
        self.chunk_repo: ChunkRepository = chunk_repo or InMemoryChunkRepository()
        self.vector_store = vector_store or VectorStore(dimension=embedding_provider.config.dimension)
        self.chunker = Chunker()
        self.manifest: dict[str, Any] = {}

    def index_directory(self, root_dir: str | Path) -> int:
        """Index all supported files under a directory.

        Args:
            root_dir: Root directory to scan for supported files.

        Returns:
            Total number of chunks added to the index.
        """
        root_path = Path(root_dir)
        
        # Use existing GraphBuilder to get semantic entities
        builder = GraphBuilder()
        builder.build_from_directory(root_path)
        
        # Extract files from scanner
        scanner = Scanner(root_path)
        files = scanner.scan_all()
        
        total_chunks = 0
        for file_info in files:
            # Build ParseResult-like data or use parser directly
            # For simplicity in this Task, we use the Chunker which can take a ParseResult or we can adapt it.
            # I'll use the graph builder's internal logic or build the parse results first.
            
            # Re-parse file to get entities (ideally we reuse builder.entities but we need them per file)
            # For now, let's assume we use the PythonParser etc via a helper
            parse_result = builder._parse_file(file_info)
            chunks = self.chunker.process_parse_result(parse_result)
            
            if not chunks:
                continue
                
            # Process embeddings in batches
            contents = [c.content for c in chunks]
            embeddings = self.embedding_provider.embed(contents)
            
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb
                self.chunk_repo.add(chunk)
                self.vector_store.add(chunk.id, emb)
                total_chunks += 1
                
        return total_chunks

    def save(self, path: str | Path) -> None:
        """Persist vector index and chunk metadata to disk.

        Args:
            path: Directory path to write index files into.
        """
        import json
        import time
        from dataclasses import asdict

        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)

        # Save vector store
        self.vector_store.save(path / "vectors")

        # Save chunk metadata via repository
        self.chunk_repo.save(path)

        # Save index manifest for compatibility checks at query time.
        self.manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "version": 1,
            "created_at": int(time.time()),
            "embedding": asdict(self.embedding_provider.config),
            "chunking": asdict(self.chunker.config),
        }
        with open(path / "index_manifest.json", "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, indent=2)

    def load(self, path: str | Path) -> None:
        """Load the entire vector index and chunk metadata from disk.

        Args:
            path: Directory path containing previously saved index files.
        """
        path = Path(path)
        
        # Load vector store
        self.vector_store.load(path / "vectors")
        
        # Load manifest (optional, for compatibility checks).
        import json
        manifest_file = path / "index_manifest.json"
        if manifest_file.exists():
            with open(manifest_file, "r", encoding="utf-8") as f:
                loaded_manifest = json.load(f)
            if not isinstance(loaded_manifest, dict):
                raise ValueError(
                    f"Invalid index manifest format in {manifest_file}. "
                    "Expected a JSON object."
                )
            self.manifest = self._validate_and_migrate_manifest(loaded_manifest)
        else:
            self.manifest = {
                "schema_version": self.SCHEMA_VERSION,
                "version": self.LEGACY_MANIFEST_VERSION,
            }

        # Load chunks via repository
        self.chunk_repo.load(path)

    @classmethod
    def _validate_and_migrate_manifest(cls, manifest: dict[str, Any]) -> dict[str, Any]:
        """Validate manifest schema and migrate legacy payloads."""
        return cls._validate_and_migrate_payload_schema(
            manifest,
            payload_name="index manifest",
            legacy_version_field="version",
            legacy_version=cls.LEGACY_MANIFEST_VERSION,
        )

    @classmethod
    def _validate_and_migrate_chunks_payload(
        cls, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate chunks payload schema and migrate legacy payloads."""
        return cls._validate_and_migrate_payload_schema(
            payload,
            payload_name="chunks metadata",
            legacy_version=cls.LEGACY_MANIFEST_VERSION,
        )

    @classmethod
    def _validate_and_migrate_payload_schema(
        cls,
        payload: dict[str, Any],
        *,
        payload_name: str,
        legacy_version_field: Optional[str] = None,
        legacy_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Validate schema metadata for an index payload and migrate when safe."""
        schema_version = payload.get("schema_version")
        if schema_version is None:
            if legacy_version_field is not None:
                legacy_value = payload.get(legacy_version_field)
                allowed_legacy_values: set[Any] = {None}
                if legacy_version is not None:
                    allowed_legacy_values.add(legacy_version)
                    allowed_legacy_values.add(str(legacy_version))
                if legacy_value not in allowed_legacy_values:
                    raise ValueError(
                        f"Unsupported legacy {payload_name} version "
                        f"{legacy_value!r}. Rebuild with `knowcode build`."
                    )
            migrated = dict(payload)
            migrated["schema_version"] = cls.SCHEMA_VERSION
            return migrated

        normalized = cls._normalize_schema_version(schema_version)
        if legacy_version is not None and normalized == legacy_version:
            migrated = dict(payload)
            migrated["schema_version"] = cls.SCHEMA_VERSION
            return migrated
        if normalized in cls.SUPPORTED_SCHEMA_VERSIONS:
            migrated = dict(payload)
            migrated["schema_version"] = normalized
            return migrated

        raise ValueError(
            f"Unsupported {payload_name} schema version "
            f"{schema_version!r}. Supported versions: "
            f"{sorted(cls.SUPPORTED_SCHEMA_VERSIONS)}. "
            "Rebuild with `knowcode build`."
        )

    @staticmethod
    def _normalize_schema_version(value: Any) -> int:
        """Normalize schema version values represented as int/str."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        raise ValueError(f"Invalid index schema version value: {value!r}")
                    
    def remove_file(self, file_path: str | Path) -> None:
        """Remove all chunks associated with a file path from the repository and index.

        Args:
            file_path: Path of the file to remove.
        """
        file_path_str = str(Path(file_path).resolve())
        removed_ids = self.chunk_repo.remove_by_file(file_path_str)
        if removed_ids:
            # Rebuild the vector store from remaining chunks if embeddings are available.
            remaining_chunks = self.chunk_repo.get_all()
            if any(c.embedding for c in remaining_chunks):
                self.vector_store.clear()
                for c in remaining_chunks:
                    if c.embedding:
                        self.vector_store.add(c.id, c.embedding)

    def index_file(self, file_path: str | Path) -> int:

        """Index a single file for incremental updates.

        Args:
            file_path: File path to process.

        Returns:
            Number of chunks created for the file.
        """
        file_path = Path(file_path)
        builder = GraphBuilder()
        from knowcode.indexing.scanner import FileInfo
        file_info = FileInfo(file_path, str(file_path), file_path.suffix, file_path.stat().st_size)
        parse_result = builder._parse_file(file_info)
        chunks = self.chunker.process_parse_result(parse_result)
        
        if chunks:
            contents = [c.content for c in chunks]
            embeddings = self.embedding_provider.embed(contents)
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb
                self.chunk_repo.add(chunk)
                self.vector_store.add(chunk.id, emb)
        return len(chunks)
