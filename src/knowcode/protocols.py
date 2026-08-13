"""Protocol interfaces that decouple service/retrieval components."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TYPE_CHECKING, runtime_checkable

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


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """Contract for dense vector index implementations.

    Step 10 (ADR 7) freezes the vector mutation and persistence contract shared
    by the FAISS/NumPy and LanceDB backends. Every member below is a *required*
    operation; ``hasattr`` is not capability negotiation, and a missing member
    is a :class:`~knowcode.errors.VectorContractError`.

    Locking and visibility contract (canonical task 3):

    * ``add``, ``upsert``, ``remove``, and ``clear`` are **serialized** mutation
      operations. A backend owns one mutation lock; they never run concurrently
      with each other or with a concurrent ``search``/``get_embedding``/
      ``count`` that observes their partial state.
    * ``search``, ``get_embedding``, and ``count`` are **snapshot-safe** reads.
      A read observes one consistent generation; a buffered or in-flight write
      is either fully visible or not visible at all. ``flush()`` is the boundary
      that makes buffered writes visible to reads.
    * ``save`` and ``load`` are **serialized** with mutation and with each
      other; they never race a concurrent ``add``/``remove``/``flush``.

    The FAISS/NumPy backend satisfies this today because FAISS commits each
    ``add`` immediately and has no buffer; LanceDB must ``flush()`` its mutable
    write buffer before any read, save, remove, or count to make "when is a
    write visible" defined. The per-generation reader-lease handoff,
    ``close()``, and generation IDs are declared here as intent and are
    implemented in Steps 11/12/18 (Step 10 does not change the artifact format).
    """

    dimension: int

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding to the index.

        Serialized mutation. Raises :class:`~knowcode.errors.VectorDimensionError`
        when ``len(embedding) != self.dimension``. Adding an ID that already
        exists is a backend-specific duplicate case (see ``upsert`` for the
        exact-ID idempotent path).
        """

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        """Exact-ID idempotent add-or-replace.

        Serialized mutation. Equivalent to ``remove(chunk_id)`` followed by
        ``add(chunk_id, embedding)`` but atomic with respect to the mutation
        lock: after it returns, exactly one live row exists for ``chunk_id``
        carrying ``embedding``, and ``count()`` reflects that. Raises
        :class:`~knowcode.errors.VectorDimensionError` on a dimension mismatch.
        """

    def search(self, embedding: list[float], limit: int = 10) -> list[tuple[str, float]]:
        """Search for similar chunk IDs and similarity scores.

        Snapshot-safe read: returns only IDs from one consistent generation,
        never an in-flight or buffered write that has not been flushed. Search
        result IDs are unique; an empty index returns ``[]``.
        """

    def get_embedding(self, chunk_id: str) -> list[float] | None:
        """Get an embedding by chunk_id.

        Snapshot-safe read. Returns ``None`` when the ID is absent. Repository
        IDs are data, never executable filter syntax, so a hostile ID must
        match itself exactly and never widen to another row.
        """

    def flush(self) -> None:
        """Make buffered writes visible to reads.

        Serialized with mutation. For backends with no write buffer (FAISS/NumPy)
        this is a no-op; for LanceDB it drains the mutable buffer into the
        durable table. Reads are only required to observe writes that have been
        flushed.
        """

    def save(self, path: Path) -> None:
        """Persist vector index artifacts.

        Serialized with mutation; flushes buffered writes first. The on-disk
        metadata envelope records the contract version, dimension, and
        backend; an incompatible artifact is rejected with
        :class:`~knowcode.errors.VectorArtifactVersionError` (Steps 11/12
        stamp and validate the version).
        """

    def load(self, path: Path) -> None:
        """Load vector index artifacts.

        Serialized with mutation. Rejects a missing, malformed, newer, or
        unsupported version rather than silently stamping the current one.
        """

    def clear(self) -> None:
        """Reset in-memory vector index data.

        Serialized mutation; removes every live row.
        """

    def remove(self, chunk_id: str) -> None:
        """Remove a chunk from the index by its exact ID.

        Serialized mutation; flushes buffered writes first. Removing an absent
        ID is a no-op. A removed ID cannot occupy a search top-k slot or bias
        fusion scores.
        """

    def count(self) -> int:
        """Return the number of live vectors in the index.

        Snapshot-safe read; reflects exactly the IDs searchable by ``search``.
        """


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
