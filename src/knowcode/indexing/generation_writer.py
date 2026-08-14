"""Derive one generation from another by mutating a staged copy (Step 18b).

Step 14 made a *full rebuild* stage every artifact and publish a pointer last.
Step 15 made a *single file update* an all-or-nothing transaction. What sat
between them was the watch worker: it committed Step 15 transactions directly
into the artifacts of the **published** generation, which ADR 4 declares
immutable and whose manifest records digests over exactly those artifacts.

Reproduced against the pre-Step-18b service, with no fault injection: after one
watched edit, ``validate_generation(..., verify_digests=True)`` on the current
generation reported a chunk-id digest mismatch and a chunk-count mismatch, and
``knowcode doctor`` failed on it. With the FAISS/NumPy backend the edit was not
durable either — ``KnowCodeService.flush()`` correctly refuses to rewrite
``vectors.*`` inside a published generation — so a restart found two chunks
beside one vector. On LanceDB the table *did* change underneath its own
envelope, so the restart failed closed with ``VectorArtifactVersionError``.

:class:`StagedGenerationWriter` closes that gap. It copies a published
generation into a staging directory no reader can resolve, applies Step 15 file
transactions there, and publishes the result as a new generation through the
same validate-then-move-the-pointer path a build uses. The published original
is read once and never written.

Publication is *compare-and-swap*: the staged generation is only a correct
successor to the one it was seeded from, so publication refuses (
:class:`~knowcode.indexing.generations.GenerationConflictError`) if the pointer
moved meanwhile rather than reverting whoever published in between.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from knowcode.indexing import generations
from knowcode.indexing.generations import (
    GenerationManifest,
    ResolvedGeneration,
)
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


def build_semantic_manifest(
    staging: Path,
    *,
    generation_id: str,
    kind: str,
    entity_ids: Sequence[str],
    relationship_count: int,
    chunk_ids: Sequence[str],
    vector_count: int,
    provider: Any,
    backend: str,
) -> GenerationManifest:
    """Describe a staged generation, including the schema versions it depends on.

    Shared by the full build and the watch batch so the two cannot drift into
    describing the same artifacts differently — a generation whose recorded
    chunk schema disagrees with the file it names fails closed on load, and
    finding that out from two constructors is strictly worse than one.
    """
    from knowcode.indexing.indexer import Indexer
    from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

    return generations.build_manifest(
        staging,
        generation_id=generation_id,
        kind=kind,
        entity_ids=entity_ids,
        relationship_count=relationship_count,
        chunk_ids=chunk_ids,
        vector_count=vector_count,
        embedding=_embedding_metadata(provider),
        vector={"backend": backend, "dimension": provider.config.dimension},
        schema_versions={
            "chunks": SqliteChunkRepository.SCHEMA_VERSION,
            "index_manifest": Indexer.SCHEMA_VERSION,
        },
    )


def _embedding_metadata(provider: Any) -> dict[str, Any]:
    """Serialize the embedding configuration recorded in a generation."""
    from dataclasses import asdict

    return dict(asdict(provider.config))


class StagedGenerationWriter:
    """A successor generation under construction, seeded from a published one.

    The writer owns its staging directory and the chunk repository and vector
    store inside it, and it is single-use: :meth:`publish` and :meth:`discard`
    both consume it, because the staging directory it wrote into either becomes
    a generation or ceases to exist.
    """

    def __init__(
        self,
        *,
        index_root: str | Path,
        base: ResolvedGeneration,
        provider: Any,
        backend: str,
    ) -> None:
        """Seed a staging generation from ``base`` and open it for writing.

        Args:
            index_root: Artifact root holding ``generations/`` and the pointer.
            base: The published generation this one succeeds. Its artifacts are
                copied, never opened for writing.
            provider: Embedding provider for the file transactions committed
                here. Its configuration is recorded in the manifest.
            backend: Configured vector backend, recorded in the manifest.

        Raises:
            ValueError: ``base`` carries no semantic index, so there is nothing
                to derive a chunk/vector generation from.
        """
        if not base.has_semantic_index:
            raise ValueError(
                f"Generation {base.generation_id} has no semantic index to "
                "derive a watch generation from."
            )

        from knowcode.indexing.indexer import Indexer
        from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
        from knowcode.storage.vector_backends import create_vector_store

        self.base = base
        self.backend = backend
        self._index_root = Path(index_root)
        self._provider = provider
        self._closed = False

        self.generation_id = generations.new_generation_id()
        self.path = generations.stage_generation(self._index_root, self.generation_id)
        try:
            copied = generations.copy_generation_artifacts(
                base.path,
                self.path,
                (generations.KNOWLEDGE_DB,) + generations.SEMANTIC_ARTIFACTS,
            )
            self._assert_seeded(copied)
            chunk_repo = SqliteChunkRepository(self.path / generations.CHUNKS_DB)
            vector_store = create_vector_store(
                backend, dimension=provider.config.dimension, index_dir=self.path
            )
            self.indexer = Indexer(
                provider, chunk_repo=chunk_repo, vector_store=vector_store
            )
            self.indexer.load(self.path)
        except BaseException:
            generations.discard_staging(self.path)
            raise

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"StagedGenerationWriter(generation_id={self.generation_id!r}, "
            f"base={self.base.generation_id!r}, closed={self._closed})"
        )

    @property
    def base_generation_id(self) -> str:
        """The published generation this one must succeed, or nothing."""
        return self.base.generation_id

    # -- transactions ----------------------------------------------------

    def replace_file(self, file_path: str | Path) -> Any:
        """Replace one file's chunks and vectors inside the staged generation."""
        self._assert_open()
        return self.indexer.replace_file(file_path)

    def delete_file(self, file_path: str | Path) -> Any:
        """Remove one file's chunks and vectors from the staged generation."""
        self._assert_open()
        return self.indexer.delete_file(file_path)

    # -- publication -----------------------------------------------------

    def publish(
        self, *, protect: Sequence[str] = (), expect_current: Optional[str] = None
    ) -> ResolvedGeneration:
        """Validate the staged generation and publish it. Consumes the writer.

        The chunk repository is closed *before* its ids are digested: an open
        write-ahead log means the committed rows are not all in ``chunks.db``
        yet, and a manifest digested over a partial file would fail closed on
        the next reader.

        Args:
            protect: Generation ids a reader still holds open (Step 18).
            expect_current: The generation this one succeeds. Publication
                refuses if the pointer no longer names it.

        Raises:
            GenerationValidationError: The staged set is not self-consistent.
            GenerationConflictError: Someone published in between.
        """
        self._assert_open()
        try:
            self.indexer.save(self.path)
            vector_count = self.indexer.vector_store.count()
            self.indexer.chunk_repo.close()

            chunk_ids = generations.read_chunk_ids(self.path / generations.CHUNKS_DB)
            manifest = build_semantic_manifest(
                self.path,
                generation_id=self.generation_id,
                kind=generations.KIND_FULL,
                entity_ids=generations.read_entity_ids(
                    self.path / generations.KNOWLEDGE_DB
                ),
                # ``knowledge.db`` was copied unchanged: a file transaction
                # rewrites chunks and vectors, never the graph.
                relationship_count=self.base.manifest.counts.get("relationships", 0),
                chunk_ids=chunk_ids,
                vector_count=vector_count,
                provider=self._provider,
                backend=self.backend,
            )
            published = generations.publish_generation(
                self._index_root,
                self.path,
                manifest,
                protect=protect,
                expect_current=expect_current,
            )
        except BaseException:
            # The staging directory is this writer's alone, so nothing survives
            # a failed publication — including a conflict, whose caller
            # re-derives its work on the new current generation instead.
            self.discard()
            raise

        self._release_stores()
        self._closed = True
        return published

    def discard(self) -> None:
        """Close the staged stores and remove the staging directory. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._release_stores()
        generations.discard_staging(self.path)

    @property
    def is_closed(self) -> bool:
        """Whether this writer has been published or discarded."""
        return self._closed

    # -- internals -------------------------------------------------------

    def _assert_seeded(self, copied: Sequence[str]) -> None:
        """Reject a base whose artifacts are not actually on disk.

        A base directory that has been deleted — retired by another writer's
        retention, or removed by hand — copies *nothing*, and every store
        opened on the staging directory afterwards would helpfully create an
        empty one. Publishing that would replace a populated generation with an
        empty one and call it a successor, so it fails closed here instead.

        Raises:
            GenerationValidationError: The base is missing artifacts a full
                generation must have.
        """
        present = set(copied)
        missing = [
            f"missing artifact {name}"
            for name in (
                generations.KNOWLEDGE_DB,
                generations.CHUNKS_DB,
                generations.VECTOR_METADATA,
            )
            if name not in present
        ]
        if not present & set(generations.NATIVE_VECTOR_ARTIFACTS):
            missing.append(
                "missing a native vector artifact "
                f"({', '.join(generations.NATIVE_VECTOR_ARTIFACTS)})"
            )
        if missing:
            raise generations.GenerationValidationError(self.base.path, missing)

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                "This StagedGenerationWriter was published or discarded; "
                "stage another one."
            )

    def _release_stores(self) -> None:
        """Close the staged stores, reporting rather than propagating failures.

        A store that fails to close must not skip the ones after it: leaking
        the rest is strictly worse than leaking the one.
        """
        for resource, what in (
            (self.indexer.chunk_repo, "staged chunk repository"),
            (self.indexer.vector_store, "staged vector store"),
        ):
            try:
                resource.close()
            except Exception:  # noqa: BLE001 - reported, and the next one closes
                logger.exception("Closing the %s failed", what)
