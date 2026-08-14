"""Indexing pipeline for code chunks."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from knowcode.data_models import CodeChunk, ParseResult
from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.indexing.chunker import Chunker
from knowcode.indexing.file_updates import (
    FileMoveCommit,
    FileUpdateCommit,
    FileUpdateCommitError,
    FileUpdatePreparationError,
    PreparedFileUpdate,
    validate_prepared_chunks,
)
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.scanner import FileInfo, Scanner
from knowcode.protocols import EmbeddingProviderProtocol, VectorStoreProtocol
from knowcode.utils.atomic_write import atomic_write_json, cleanup_orphaned_temp_files
from knowcode.utils.entity_identity import normalize_file_identity
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


class Indexer:
    """Orchestrates scan -> chunk -> embed -> index pipeline.

    Every mutation of an existing index goes through one primitive: prepare a
    complete replacement for a file (no live state touched), then commit it as
    one transaction across the chunk repository and the vector store (Step 15).
    ``index_file``, ``remove_file``, ``move_file``, ``index_directory``, and
    ``index_incremental`` are all thin wrappers over it, so no caller can
    reintroduce the remove-before-index sequence that lost a file's generation
    whenever embedding failed.
    """

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
        self.chunk_repo: ChunkRepository = chunk_repo or SqliteChunkRepository(
            ":memory:", dimension=embedding_provider.config.dimension
        )
        self.vector_store: VectorStoreProtocol
        if vector_store is None:
            from knowcode.storage.vector_store import VectorStore
            self.vector_store = VectorStore(dimension=embedding_provider.config.dimension)
        else:
            self.vector_store = vector_store
        self.chunker = Chunker()
        self.manifest: dict[str, Any] = {}
        # One commit at a time: a file's chunk transaction and its matching
        # vector writes must not interleave with another file's.
        self._commit_lock = threading.Lock()
        self._file_parser: Optional[GraphBuilder] = None
        #: Files a bulk pipeline could not replace, as (identity, reason).
        #: Each kept its previous generation rather than being dropped.
        self.failed_updates: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # File update transactions (Step 15)
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Embedding width every committed chunk must carry."""
        return self.vector_store.dimension

    def prepare_file_update(
        self,
        file_path: str | Path,
        *,
        parse_result: Optional[ParseResult] = None,
        reuse_embeddings: bool = True,
    ) -> PreparedFileUpdate:
        """Build a complete, validated replacement for one file. No mutation.

        Parsing, chunking, and embedding all happen here, against the file on
        disk and the durable embeddings already stored for unchanged content.
        The live chunk repository and vector store are read but never written,
        so any failure — an unreadable file, a provider outage, a short or
        wrong-width embedding batch — leaves the previous generation exactly as
        it was.

        Args:
            file_path: File to prepare an update for. Normalized to the
                canonical identity (ADR 1), so a symlink or ``..`` alias
                targets the rows stored for the resolved source file.
            parse_result: An already-parsed result for this file. A generation
                build passes the parse its graph was built from so one parse
                feeds both artifacts.
            reuse_embeddings: Look up durable embeddings for chunks whose
                content is unchanged. A full build into an empty repository
                passes ``False``: nothing can match, and the per-chunk hash
                lookup is pure cost.

        Raises:
            FileUpdatePreparationError: The replacement could not be built or
                validated. Nothing was mutated.
        """
        file_identity = normalize_file_identity(file_path)
        path = Path(file_identity)

        if parse_result is None:
            if not path.is_file() or path.suffix not in Scanner.SUPPORTED_EXTENSIONS:
                # A deleted or no-longer-indexable file prepares as a deletion
                # rather than an error: that is exactly what a watch delete
                # event and a removed-in-git file need to commit.
                return PreparedFileUpdate(file_identity, (), self.dimension)
            parse_result = self._parse_one_file(path)

        chunks = self.chunker.process_parse_result(parse_result)
        parse_errors = tuple(parse_result.errors)
        if not chunks:
            if parse_errors and path.is_file():
                # A file that still exists but produced nothing extractable was
                # not successfully replaced — it was probably saved mid-edit
                # with a syntax error. Committing this as a deletion would drop
                # the file out of the index until the next event.
                raise FileUpdatePreparationError(file_identity, list(parse_errors))
            return PreparedFileUpdate(
                file_identity, (), self.dimension, parse_errors=parse_errors
            )

        reused = self._reuse_durable_embeddings(chunks) if reuse_embeddings else 0
        pending = [chunk for chunk in chunks if chunk.embedding is None]
        if pending:
            self._embed_pending(file_identity, pending)

        validate_prepared_chunks(file_identity, chunks, self.dimension)
        return PreparedFileUpdate(
            file_path=file_identity,
            chunks=tuple(chunks),
            dimension=self.dimension,
            reused_embeddings=reused,
            embedded_chunks=len(pending),
            parse_errors=parse_errors,
        )

    def commit_file_update(self, update: PreparedFileUpdate) -> FileUpdateCommit:
        """Commit a prepared replacement across both stores as one transaction.

        The chunk repository replaces the file's whole generation in one writer
        transaction; the vector store then upserts every committed id and
        removes the ids that did not survive. A vector failure after the SQLite
        commit is repaired from the durable embeddings the chunk store now
        holds, because those rows — not the provider and not the previous
        vector state — are the committed truth.

        Raises:
            FileUpdatePreparationError: The update is not committable. Checked
                again here, before the transaction, so a hand-built or replayed
                update cannot reach the stores unvalidated.
            FileUpdateCommitError: The vector state could not be reconciled
                with the committed chunks. The caller's generation is
                inconsistent and must be discarded or rebuilt.
        """
        validate_prepared_chunks(update.file_path, update.chunks, self.dimension)

        with self._commit_lock:
            replacement = self.chunk_repo.replace_file(
                update.file_path, list(update.chunks)
            )
            committed = set(replacement.committed_chunk_ids)
            removed = tuple(
                chunk_id
                for chunk_id in replacement.previous_chunk_ids
                if chunk_id not in committed
            )

            recovered = False
            try:
                self._apply_vector_generation(update.chunks, removed)
            except Exception as exc:  # noqa: BLE001 - recovered or re-raised
                logger.warning(
                    "Vector update for %s failed after its chunks committed: %s",
                    update.file_path,
                    exc,
                )
                if not self._recover_vectors_from_chunks(
                    replacement.committed_chunk_ids, removed
                ):
                    raise FileUpdateCommitError(update.file_path, str(exc)) from exc
                recovered = True

            return FileUpdateCommit(
                file_path=replacement.file_path,
                previous_chunk_ids=replacement.previous_chunk_ids,
                committed_chunk_ids=replacement.committed_chunk_ids,
                removed_chunk_ids=removed,
                generation_metadata=replacement.generation_metadata,
                recovered=recovered,
            )

    def replace_file(
        self,
        file_path: str | Path,
        *,
        parse_result: Optional[ParseResult] = None,
        reuse_embeddings: bool = True,
    ) -> FileUpdateCommit:
        """Prepare and commit one file's replacement generation."""
        update = self.prepare_file_update(
            file_path, parse_result=parse_result, reuse_embeddings=reuse_embeddings
        )
        return self.commit_file_update(update)

    def delete_file(self, file_path: str | Path) -> FileUpdateCommit:
        """Remove one file's chunks and their vectors as one transaction."""
        update = PreparedFileUpdate(
            normalize_file_identity(file_path), (), self.dimension
        )
        return self.commit_file_update(update)

    def move_file(
        self, old_path: str | Path, new_path: str | Path
    ) -> FileMoveCommit:
        """Move one file's generation to a new canonical identity.

        The destination is prepared and committed *before* the source is
        dropped, so an embedding failure leaves the source searchable and the
        worst post-commit outcome is a duplicate a retry clears.
        """
        committed = self.replace_file(new_path)
        old_identity = normalize_file_identity(old_path)
        if old_identity == committed.file_path:
            return FileMoveCommit(
                committed=committed,
                removed=FileUpdateCommit(file_path=old_identity),
            )
        return FileMoveCommit(committed=committed, removed=self.delete_file(old_path))

    # -- internals -------------------------------------------------------

    def _parse_one_file(self, path: Path) -> ParseResult:
        """Parse a single file, reusing one parser set across calls."""
        if self._file_parser is None:
            # GraphBuilder owns the per-language parsers; ``_parse_file`` only
            # dispatches, so one instance can be reused as a parser pool
            # without accumulating graph state.
            self._file_parser = GraphBuilder()
        file_info = FileInfo(path, str(path), path.suffix, path.stat().st_size)
        return self._file_parser._parse_file(file_info)

    def _reuse_durable_embeddings(self, chunks: Sequence[CodeChunk]) -> int:
        """Attach embeddings already stored for unchanged chunk content.

        The durable chunk row is the source (ADR 3). The vector store is only
        consulted when a row predates durable embeddings, so preparation never
        depends on transient state.
        """
        reused = 0
        for chunk in chunks:
            content_hash = chunk.metadata.get("content_hash")
            if not content_hash:
                continue
            cached_id = self.chunk_repo.get_chunk_id_by_hash(content_hash)
            if not cached_id:
                continue
            cached = self.chunk_repo.get(cached_id)
            embedding = cached.embedding if cached is not None else None
            if embedding is None:
                embedding = self.vector_store.get_embedding(cached_id)
            if embedding is None:
                continue
            chunk.embedding = list(embedding)
            reused += 1
        return reused

    def _embed_pending(self, file_identity: str, pending: list[CodeChunk]) -> None:
        """Embed the chunks with no reusable vector, in one batch."""
        try:
            embeddings = self.embedding_provider.embed(
                [chunk.content for chunk in pending]
            )
        except Exception as exc:  # noqa: BLE001 - reported as a preparation failure
            raise FileUpdatePreparationError(
                file_identity, [f"embedding failed: {exc}"]
            ) from exc

        if len(embeddings) != len(pending):
            raise FileUpdatePreparationError(
                file_identity,
                [
                    f"embedding provider returned {len(embeddings)} vectors "
                    f"for {len(pending)} chunks"
                ],
            )
        for chunk, embedding in zip(pending, embeddings):
            chunk.embedding = list(embedding)

    def _replace_or_report(
        self, file_path: str | Path, *, parse_result: Optional[ParseResult] = None,
        reuse_embeddings: bool = True,
    ) -> int:
        """Replace one file inside a bulk pipeline, keeping it on failure.

        A bulk build must not abort because one file was saved mid-edit: that
        file simply keeps whatever generation it already had. The failure is
        recorded in :attr:`failed_updates` and logged rather than swallowed,
        because a silently skipped file looks identical to an up-to-date one.
        """
        try:
            commit = self.replace_file(
                file_path,
                parse_result=parse_result,
                reuse_embeddings=reuse_embeddings,
            )
        except FileUpdatePreparationError as exc:
            logger.warning("Keeping the previous index generation for %s: %s",
                           exc.file_path, "; ".join(exc.reasons))
            self.failed_updates.append((exc.file_path, "; ".join(exc.reasons)))
            return 0
        return commit.chunk_count

    def _apply_vector_generation(
        self, chunks: Sequence[CodeChunk], removed_ids: Sequence[str]
    ) -> None:
        """Make the vector store match the chunks just committed."""
        for chunk in chunks:
            embedding = chunk.embedding
            if embedding is None:
                # Unreachable: both preparation and commit validate first.
                # Raising rather than skipping keeps a membership split
                # impossible even if a future caller bypasses them.
                raise FileUpdateCommitError(
                    chunk.id, "committed chunk carries no embedding"
                )
            self.vector_store.upsert(chunk.id, embedding)
        for chunk_id in removed_ids:
            self.vector_store.remove(chunk_id)
        self.vector_store.flush()

    def _recover_vectors_from_chunks(
        self, committed_ids: Sequence[str], removed_ids: Sequence[str]
    ) -> bool:
        """Rebuild the affected vectors from the durable chunk rows.

        Returns ``False`` when the vector store still disagrees with the
        committed chunks, which makes the whole update a commit failure.
        """
        try:
            for chunk_id in removed_ids:
                self.vector_store.remove(chunk_id)
            for chunk_id in committed_ids:
                chunk = self.chunk_repo.get(chunk_id)
                if chunk is None or chunk.embedding is None:
                    logger.error(
                        "Cannot rebuild vectors for %s: no durable embedding",
                        chunk_id,
                    )
                    return False
                self.vector_store.upsert(chunk_id, chunk.embedding)
            self.vector_store.flush()
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            logger.error("Vector recovery from durable chunks failed: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Bulk pipelines
    # ------------------------------------------------------------------

    def index_directory(
        self, root_dir: str | Path, *, builder: Optional[GraphBuilder] = None
    ) -> int:
        """Index all supported files under a directory.

        Args:
            root_dir: Root directory to scan for supported files.
            builder: An already-built graph whose scan and parse results should
                be reused. A generation build passes the same builder it wrote
                ``knowledge.db`` from, so the chunk set derives from exactly the
                parse that produced the entities (Step 14). Re-scanning here
                would apply a different ignore set.

        Returns:
            Total number of chunks added to the index.
        """
        root_path = Path(root_dir)

        if builder is None:
            builder = GraphBuilder()
            builder.build_from_directory(root_path)

        parse_results = builder.parse_results
        if not parse_results:
            # A builder supplied by an older caller (or one built from an empty
            # directory) has nothing retained; fall back to a fresh scan.
            scanner = Scanner(root_path)
            parse_results = [
                builder._parse_file(file_info) for file_info in scanner.scan_all()
            ]

        total_chunks = 0
        self.failed_updates = []
        for parse_result in parse_results:
            # One prepare/commit transaction per file, same as the incremental
            # and watch paths. Nothing can be reused in a fresh generation, so
            # the per-chunk hash lookup is skipped.
            total_chunks += self._replace_or_report(
                parse_result.file_path,
                parse_result=parse_result,
                reuse_embeddings=False,
            )

        # Store current commit hash for future incremental indexing
        try:
            import git
            repo = git.Repo(str(root_path), search_parent_directories=True)
            self.manifest["last_indexed_commit"] = repo.head.commit.hexsha
        except Exception as e:
            logger.warning("Failed to get git commit for manifest: %s", e)

        return total_chunks

    def index_incremental(self, root_dir: str | Path) -> int:
        """Incrementally index only changed files and skip re-embedding unchanged chunks.

        Args:
            root_dir: Root directory of the repository.

        Returns:
            Number of new chunks added.
        """
        root_path = Path(root_dir).resolve()
        
        # Determine last indexed commit
        last_commit = self.manifest.get("last_indexed_commit")
        
        # Get current commit
        try:
            import git
            repo = git.Repo(str(root_path), search_parent_directories=True)
            current_commit = repo.head.commit.hexsha
        except Exception as e:
            logger.warning(f"Failed to get current git commit: {e}. Incremental indexer falling back to full index.")
            return self.index_directory(root_dir)

        if not last_commit:
            logger.info("No last_indexed_commit found in manifest. Falling back to full index.")
            self.manifest["last_indexed_commit"] = current_commit
            return self.index_directory(root_dir)

        if current_commit == last_commit:
            logger.info("Current commit matches last indexed commit. No changes to index.")
            return 0

        # Get changed files
        try:
            changed_files_rel = []
            diff = repo.commit(last_commit).diff(current_commit)
            for d in diff:
                if d.b_path:
                    changed_files_rel.append(d.b_path)
            
            untracked = repo.untracked_files
            changed_files_rel.extend(untracked)
        except Exception as e:
            logger.warning(f"Failed to get git diff: {e}. Falling back to full index.")
            self.manifest["last_indexed_commit"] = current_commit
            return self.index_directory(root_dir)

        changed_files_rel = list(set(changed_files_rel))
        changed_files = [str(root_path / f) for f in set(changed_files_rel) if f.strip()]

        if not changed_files:
            self.manifest["last_indexed_commit"] = current_commit
            return 0

        total_chunks = 0
        self.failed_updates = []

        # Only the changed files are parsed. The scan supplies file identity
        # and indexability; it does not build a graph, because chunking needs
        # the per-file parse result and nothing else.
        file_map = {f.path.resolve(): f for f in Scanner(root_path).scan_all()}

        for file_path_str in changed_files:
            file_path = Path(file_path_str).resolve()

            file_info = file_map.get(file_path)
            if file_info is None:
                # Deleted, ignored, or no longer an indexable type.
                self.delete_file(file_path_str)
                continue

            # One transaction per file: parse, chunk, and embed first, then
            # replace the file's whole generation. A failure here leaves the
            # file's previous chunks and vectors searchable.
            total_chunks += self._replace_or_report(
                file_path_str, parse_result=self._parse_one_file(file_info.path)
            )

        self.manifest["last_indexed_commit"] = current_commit
        return total_chunks

    def save(self, path: str | Path) -> None:
        """Persist vector index and chunk metadata to disk.

        Writes data before metadata: vectors, then chunks, then the manifest
        that describes both (Step 13 publication ordering).

        Args:
            path: Directory path to write index files into.
        """
        import time
        from dataclasses import asdict

        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)

        # Save vector store
        self.vector_store.save(path / "vectors")

        # Save chunk metadata via repository
        self.chunk_repo.save(path)

        # Save index manifest for compatibility checks at query time.
        self.manifest.update({
            "schema_version": self.SCHEMA_VERSION,
            "version": 1,
            "created_at": int(time.time()),
            "embedding": asdict(self.embedding_provider.config),
            "chunking": asdict(self.chunker.config),
        })
        # Manifest last, and atomically (Step 13): it describes the vectors and
        # chunks written above, so a crash may leave an older manifest beside
        # present data but never a manifest naming data that does not exist.
        atomic_write_json(path / "index_manifest.json", self.manifest, indent=2)

    def load(self, path: str | Path) -> None:
        """Load the entire vector index and chunk metadata from disk.

        Args:
            path: Directory path containing previously saved index files.
        """
        path = Path(path)

        # Startup hygiene: staging files left by a crashed writer are removed
        # before anything reads the directory (Step 13).
        cleanup_orphaned_temp_files(path)

        # Load vector store
        self.vector_store.load(path / "vectors")

        # Load manifest (optional, for compatibility checks).
        import json
        manifest_file = path / "index_manifest.json"
        if manifest_file.exists():
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    loaded_manifest = json.load(f)
            except json.JSONDecodeError as exc:
                # Pre-Step-13 manifests were truncated in place, so an
                # interrupted write left exactly this file behind.
                raise ValueError(
                    f"Invalid or truncated index manifest in {manifest_file}: "
                    f"{exc}. Rebuild with `knowcode build`."
                ) from exc
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
        """Remove all chunks associated with a file path from both stores.

        Kept as the established name for callers that do not need the commit
        record; :meth:`delete_file` is the same transaction and returns it.
        """
        self.delete_file(file_path)

    def index_file(self, file_path: str | Path) -> int:
        """Replace one file's generation in the index.

        Args:
            file_path: File path to process.

        Returns:
            Number of chunks searchable for the file after the commit.
        """
        return self.replace_file(file_path).chunk_count
