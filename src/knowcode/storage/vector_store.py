"""Vector store for dense retrieval using FAISS, with a numpy fallback.

Step 11 (ADR 7) repairs the reviewed removal defect. Removal used to delete only
the ``id_map`` entry, leaving the vector in the native index: ``count()``
disagreed with ``index.ntotal``, a removed ID still consumed a search top-k slot,
and a duplicate ``add`` created two native rows for one chunk ID.

The index is now ID-aware on both engines. FAISS uses ``IndexIDMap2`` so
``remove_ids`` deletes the native row and ``reconstruct`` reads by assigned ID;
:class:`MockVectorStore` mirrors that surface with numpy. Tombstones are removed
rather than hidden by overfetching, and every mutation and read is serialized
under one lock per the :class:`~knowcode.protocols.VectorStoreProtocol` contract.
"""

import importlib
import json
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from knowcode.errors import VectorArtifactVersionError, VectorDimensionError
from knowcode.utils.atomic_write import atomic_replacement, atomic_write_json

faiss: Any | None
try:
    faiss = importlib.import_module("faiss")
except ImportError:
    # Optional dependency
    faiss = None

#: Native ID assigned to a search slot that matched no vector.
_NO_MATCH_ID = -1


class MockVectorStore:
    """Fallback index implementation when FAISS is unavailable.

    Mirrors the subset of ``faiss.IndexIDMap2`` that :class:`VectorStore` needs:
    ``add_with_ids``, ``remove_ids``, ``reconstruct``, and a ``search`` that
    returns the assigned native IDs. Removal deletes the row itself, so
    ``ntotal`` never counts a tombstone.

    ``add()`` without explicit IDs keeps assigning sequential IDs, so a store
    used positionally behaves as it did before Step 11.
    """

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.index = np.empty((0, dimension), dtype="float32")
        self.ids = np.empty((0,), dtype="int64")
        self._next_auto_id = 0

    @property
    def ntotal(self) -> int:
        """Number of live rows; derived so it cannot drift from the data."""
        return int(self.index.shape[0])

    def add(self, x: np.ndarray) -> None:
        """Append rows with sequentially assigned native IDs."""
        rows = np.asarray(x, dtype="float32")
        ids = np.arange(
            self._next_auto_id, self._next_auto_id + rows.shape[0], dtype="int64"
        )
        self.add_with_ids(rows, ids)

    def add_with_ids(self, x: np.ndarray, ids: np.ndarray) -> None:
        """Append rows carrying explicit native IDs."""
        rows = np.asarray(x, dtype="float32")
        row_ids = np.asarray(ids, dtype="int64")
        self.index = np.vstack([self.index, rows])
        self.ids = np.concatenate([self.ids, row_ids])
        if row_ids.size:
            self._next_auto_id = max(self._next_auto_id, int(row_ids.max()) + 1)

    def remove_ids(self, ids: np.ndarray) -> int:
        """Delete every row whose native ID is in ``ids``; return the count."""
        keep = ~np.isin(self.ids, np.asarray(ids, dtype="int64"))
        removed = self.ntotal - int(keep.sum())
        self.index = self.index[keep]
        self.ids = self.ids[keep]
        return removed

    def reconstruct(self, native_id: int) -> np.ndarray:
        """Return the row stored under ``native_id``."""
        positions = np.flatnonzero(self.ids == np.int64(native_id))
        if positions.size == 0:
            raise KeyError(native_id)
        return np.asarray(self.index[int(positions[0])], dtype="float32")

    def set_row_ids(self, ids: list[int]) -> None:
        """Re-associate loaded rows with their persisted native IDs."""
        row_ids = np.asarray(ids, dtype="int64")
        if row_ids.shape[0] != self.ntotal:
            raise ValueError(
                f"row id count {row_ids.shape[0]} does not match {self.ntotal} rows"
            )
        self.ids = row_ids
        self._next_auto_id = int(row_ids.max()) + 1 if row_ids.size else 0

    def search(self, x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(scores, native_ids)`` for the ``k`` closest rows."""
        if self.ntotal == 0 or k <= 0:
            return (
                np.empty((1, 0), dtype="float32"),
                np.empty((1, 0), dtype="int64"),
            )

        # Simple cosine similarity
        norm_a = np.linalg.norm(self.index, axis=1, keepdims=True)
        norm_b = np.linalg.norm(x, axis=1, keepdims=True)
        scores = np.dot(self.index, x.T) / (norm_a * norm_b.T + 1e-9)
        scores = scores.flatten()

        # Sort indices by score descending
        k = min(k, self.ntotal)
        positions = np.argsort(-scores)[:k]
        return scores[positions].reshape(1, -1), self.ids[positions].reshape(1, -1)

    def save(self, path: Path) -> None:
        """Save vectors to the exact path given.

        Written through an open handle deliberately: ``np.save`` appends
        ``.npy`` to a *filename* that lacks it, which would silently miss the
        staging path :func:`~knowcode.utils.atomic_write.atomic_replacement`
        hands out and publish an empty artifact instead.
        """
        with open(path, "wb") as handle:
            np.save(handle, self.index)

    def load(self, path: Path) -> None:
        """Load vectors from .npy file with sequential native IDs.

        ``dimension`` is adopted from the loaded rows so it always describes the
        data rather than whatever this instance was constructed with.
        :meth:`VectorStore.load` restores the persisted IDs afterwards via
        :meth:`set_row_ids`; the sequential default keeps a standalone load
        (no metadata envelope) usable.
        """
        if path.exists():
            self.index = np.load(str(path))
            self.dimension = int(self.index.shape[1])
            self.ids = np.arange(self.index.shape[0], dtype="int64")
            self._next_auto_id = self.ntotal


class VectorStore:
    """FAISS-based vector store for code embeddings with numpy fallback.

    Every operation is serialized under one re-entrant lock. ``add``, ``upsert``,
    ``remove``, ``clear``, ``save``, and ``load`` are serialized mutations;
    ``search``, ``get_embedding``, and ``count`` observe one consistent
    generation. ``flush()`` is a no-op because each mutation commits immediately.

    Chunk IDs are exact-match data: each maps to one monotonically assigned
    native ID, so adding an ID that already exists replaces its single live row
    rather than duplicating it.
    """

    #: Metadata envelope version. Bumped to 3 by Step 11: the FAISS artifact is
    #: now an ``IndexIDMap2`` and the envelope carries engine/count/generation,
    #: neither of which a v1/v2 artifact can supply.
    SCHEMA_VERSION = 3
    SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
    #: Registry key shared by both engines (see ``vector_backends``).
    BACKEND = "faiss"
    REBUILD_HINT = "Rebuild the semantic index with `knowcode build`."

    def __init__(
        self, dimension: int = 1536, index_path: Optional[Path] = None
    ) -> None:
        """Initialize the vector index.

        Args:
            dimension: Expected embedding dimensionality.
            index_path: Optional base path (without suffix) to load an existing
                index from disk, as :meth:`save` writes it.
        """
        self.dimension = dimension
        self.index_path = index_path
        self._lock = threading.RLock()
        self._generation = 0
        self._next_native_id = 0
        self.index: Any = self._new_index(dimension)
        self.id_map: dict[int, str] = {}  # native id -> chunk_id
        self._native_ids: dict[str, int] = {}  # chunk_id -> native id

        # The base path itself is never a file (save writes ``.index``/``.npy``
        # and ``.json`` beside it), so existence is tested on the artifacts.
        if index_path and self._has_artifacts(Path(index_path)):
            self.load(index_path)

    # --- engine helpers ---------------------------------------------------

    @property
    def engine(self) -> str:
        """Name of the native engine backing this store."""
        return "faiss" if faiss else "numpy"

    @staticmethod
    def _has_artifacts(path: Path) -> bool:
        """Whether any vector artifact exists beside the given base path."""
        return any(
            path.with_suffix(suffix).exists()
            for suffix in (".json", ".index", ".npy")
        )

    @staticmethod
    def _new_index(dimension: int) -> Any:
        """Create an empty ID-aware native index for the active engine."""
        if faiss:
            # Inner product over normalized vectors == cosine similarity.
            # IndexIDMap2 adds by-ID add/remove/reconstruct on top of it.
            return faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
        return MockVectorStore(dimension)

    @staticmethod
    def _native_dimension(index: Any) -> int:
        """Dimensionality the given native index actually holds."""
        return int(index.d) if faiss else int(index.dimension)

    @staticmethod
    def _native_id_set(index: Any) -> set[int]:
        """Native IDs the given index actually holds."""
        if faiss:
            return {int(value) for value in faiss.vector_to_array(index.id_map)}
        return {int(value) for value in index.ids}

    # --- mutation ---------------------------------------------------------

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding, replacing any existing row for that ID.

        Add is exact-ID add-or-replace so re-indexing a chunk cannot leave two
        native rows that both occupy result slots and bias fusion scores.
        """
        self._put(chunk_id, embedding)

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        """Exact-ID idempotent add-or-replace."""
        self._put(chunk_id, embedding)

    def _put(self, chunk_id: str, embedding: list[float]) -> None:
        """Replace ``chunk_id``'s row with ``embedding`` under the lock."""
        self._require_dimension(embedding)
        vec = np.array([embedding]).astype("float32")
        with self._lock:
            self._remove_locked(chunk_id)
            native_id = self._next_native_id
            self._next_native_id += 1
            self.index.add_with_ids(vec, np.array([native_id], dtype="int64"))
            self.id_map[native_id] = chunk_id
            self._native_ids[chunk_id] = native_id
            self._generation += 1

    def remove(self, chunk_id: str) -> None:
        """Remove a chunk from the index by its exact ID.

        The native row is deleted, so the ID cannot occupy a top-k slot.
        Removing an absent ID is a no-op.
        """
        with self._lock:
            if self._remove_locked(chunk_id):
                self._generation += 1

    def _remove_locked(self, chunk_id: str) -> bool:
        """Delete ``chunk_id``'s native row; return whether one existed."""
        native_id = self._native_ids.pop(chunk_id, None)
        if native_id is None:
            return False
        self.index.remove_ids(np.array([native_id], dtype="int64"))
        self.id_map.pop(native_id, None)
        return True

    def clear(self) -> None:
        """Clear the native index and reset the ID maps."""
        with self._lock:
            self.index = self._new_index(self.dimension)
            self.id_map = {}
            self._native_ids = {}
            self._next_native_id = 0
            self._generation += 1

    def flush(self) -> None:
        """No-op: FAISS/NumPy commits each mutation immediately, with no buffer."""
        return None

    def _require_dimension(self, embedding: list[float]) -> None:
        """Raise VectorDimensionError when the embedding length disagrees."""
        actual = len(embedding)
        if actual != self.dimension:
            raise VectorDimensionError(self.dimension, actual)

    # --- reads ------------------------------------------------------------

    def search(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """Search for similar embeddings.

        Args:
            embedding: Query embedding to search for.
            limit: Maximum number of results to return.

        Returns:
            List of (chunk_id, score) tuples with unique chunk IDs.
        """
        vec = np.array([embedding]).astype("float32")
        with self._lock:
            if limit <= 0 or self.index.ntotal == 0:
                return []
            distances, native_ids = self.index.search(vec, limit)

            results = []
            if native_ids.size > 0 and native_ids[0].size > 0:
                for dist, native_id in zip(distances[0], native_ids[0]):
                    native_id = int(native_id)
                    if native_id == _NO_MATCH_ID:
                        continue
                    chunk_id = self.id_map.get(native_id)
                    if chunk_id is not None:
                        results.append((chunk_id, float(dist)))

            return results

    def get_embedding(self, chunk_id: str) -> list[float] | None:
        """Fetch the embedding for a chunk ID, or None when it is absent."""
        with self._lock:
            native_id = self._native_ids.get(chunk_id)
            if native_id is None:
                return None
            return [float(x) for x in self.index.reconstruct(native_id).tolist()]

    def count(self) -> int:
        """Return the number of live vectors in the index."""
        with self._lock:
            return len(self.id_map)

    # --- persistence ------------------------------------------------------

    def save(self, path: Path) -> None:
        """Save the native index and the metadata envelope to disk.

        Publication order is data first, metadata last (Step 13): the envelope
        names rows, counts, and an ID map, so it must never become visible
        before the native artifact those describe. Both writes replace their
        target atomically, so a failure leaves the previously saved generation
        loadable.
        """
        path = Path(path)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)

            native_file = path.with_suffix(".index" if faiss else ".npy")
            with atomic_replacement(native_file) as staged:
                if faiss:
                    faiss.write_index(self.index, str(staged))
                else:
                    self.index.save(staged)

            payload: dict[str, Any] = {
                "schema_version": self.SCHEMA_VERSION,
                "backend": self.BACKEND,
                "engine": self.engine,
                "dimension": self.dimension,
                "count": len(self.id_map),
                "generation": self._generation,
                "next_native_id": self._next_native_id,
                "id_map": {str(k): v for k, v in self.id_map.items()},
            }
            if not faiss:
                # FAISS persists native IDs inside its own artifact; the numpy
                # fallback records the ID of each saved row in order instead.
                payload["row_ids"] = [int(value) for value in self.index.ids]

            atomic_write_json(path.with_suffix(".json"), payload)

    def load(self, path: Path) -> None:
        """Load the native index and metadata envelope, validating both.

        A path with no vector artifacts at all loads as an empty index, which is
        how a fresh :class:`~knowcode.indexing.indexer.Indexer` reads a directory
        that has never been saved. Anything else — a half-published generation,
        a legacy envelope, an artifact from the other engine, or an ID map that
        disagrees with the native rows — fails closed with rebuild guidance.
        """
        path = Path(path)
        index_file = path.with_suffix(".index")
        npy_file = path.with_suffix(".npy")
        json_file = path.with_suffix(".json")
        native_file = index_file if faiss else npy_file

        with self._lock:
            if not json_file.exists():
                if not (index_file.exists() or npy_file.exists()):
                    return  # never saved: an empty index is the correct state
                raise self._artifact_error(
                    f"vector index at {path} has no metadata envelope "
                    f"({json_file.name} is missing)"
                )

            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                # A pre-Step-13 truncate-in-place write could be interrupted
                # part-way; fail closed instead of surfacing a raw parse error.
                raise self._artifact_error(
                    f"invalid or truncated vector metadata in {json_file}: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise self._artifact_error(
                    f"invalid vector metadata in {json_file}: expected a JSON object"
                )
            metadata = self._validate_metadata(data)
            self._require_engine(metadata, json_file)

            if not native_file.exists():
                raise self._artifact_error(
                    f"vector metadata {json_file.name} has no {native_file.name} "
                    f"artifact for the {self.engine} engine"
                )

            # Prepare the whole candidate generation before touching live
            # state: a rejected artifact must leave this store exactly as it
            # was, never half-swapped onto a validated index with a stale map.
            candidate = self._read_native(native_file)
            id_map = self._load_id_map(metadata, json_file)
            dimension = self._validated_dimension(metadata, candidate, json_file)
            if not faiss:
                self._apply_row_ids(candidate, metadata, json_file)
            self._require_native_parity(candidate, metadata, id_map, json_file)

            self.index = candidate
            self.dimension = dimension
            self.id_map = id_map
            self._native_ids = {chunk_id: k for k, chunk_id in id_map.items()}
            self._next_native_id = self._resolved_next_native_id(metadata, id_map)
            self._generation = int(metadata.get("generation", 0))

    def _read_native(self, native_file: Path) -> Any:
        """Read the engine's native artifact into a detached index."""
        if faiss:
            return faiss.read_index(str(native_file))
        index = MockVectorStore(self.dimension)
        index.load(native_file)
        return index

    def _load_id_map(self, metadata: dict[str, Any], source: Path) -> dict[int, str]:
        """Parse and validate the persisted native-id -> chunk-id mapping."""
        raw = metadata.get("id_map", {})
        if not isinstance(raw, dict):
            raise self._artifact_error(
                f"invalid vector metadata in {source}: 'id_map' must be an object"
            )
        try:
            id_map = {int(k): str(v) for k, v in raw.items()}
        except (TypeError, ValueError) as exc:
            raise self._artifact_error(
                f"invalid vector metadata in {source}: unreadable id_map ({exc})"
            ) from exc

        chunk_ids = list(id_map.values())
        if len(set(chunk_ids)) != len(chunk_ids):
            raise self._artifact_error(
                f"invalid vector metadata in {source}: duplicate chunk IDs map to "
                "more than one native vector"
            )
        return id_map

    def _require_engine(self, metadata: dict[str, Any], source: Path) -> None:
        """Reject an artifact written by the other native engine."""
        recorded = metadata.get("engine")
        if recorded != self.engine:
            raise self._artifact_error(
                f"vector artifact {source.name} was written by the {recorded!r} "
                f"engine but this process uses {self.engine!r}"
            )

    def _validated_dimension(
        self, metadata: dict[str, Any], candidate: Any, source: Path
    ) -> int:
        """Return the persisted dimension once the native index agrees with it."""
        recorded = metadata.get("dimension")
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            raise self._artifact_error(
                f"invalid vector metadata in {source}: 'dimension' must be an integer"
            )
        native = self._native_dimension(candidate)
        if recorded != native:
            raise self._artifact_error(
                f"vector metadata {source.name} declares dimension {recorded} but "
                f"its index holds {native}-dimensional vectors"
            )
        return recorded

    def _require_native_parity(
        self,
        candidate: Any,
        metadata: dict[str, Any],
        id_map: dict[int, str],
        source: Path,
    ) -> None:
        """Prove the ID map and the native rows describe the same vectors."""
        native_ids = self._native_id_set(candidate)
        if native_ids != set(id_map):
            raise self._artifact_error(
                f"vector metadata {source.name} lists {len(id_map)} mapped vectors "
                f"but its index holds {len(native_ids)} native vectors; the live "
                "count and the ID map have drifted apart"
            )

        recorded_count = metadata.get("count")
        if recorded_count is not None and recorded_count != len(id_map):
            raise self._artifact_error(
                f"vector metadata {source.name} declares count {recorded_count} but "
                f"maps {len(id_map)} vectors"
            )

    def _apply_row_ids(
        self, candidate: Any, metadata: dict[str, Any], source: Path
    ) -> None:
        """Restore persisted native IDs onto numpy-fallback rows."""
        row_ids = metadata.get("row_ids")
        if not isinstance(row_ids, list):
            raise self._artifact_error(
                f"invalid vector metadata in {source}: the numpy engine requires "
                "'row_ids' to map saved rows to native IDs"
            )
        try:
            candidate.set_row_ids([int(value) for value in row_ids])
        except (TypeError, ValueError) as exc:
            raise self._artifact_error(
                f"invalid vector metadata in {source}: unusable row_ids ({exc})"
            ) from exc

    @staticmethod
    def _resolved_next_native_id(
        metadata: dict[str, Any], id_map: dict[int, str]
    ) -> int:
        """Resume ID assignment above every persisted native ID."""
        highest = max(id_map) + 1 if id_map else 0
        recorded = metadata.get("next_native_id")
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return max(recorded, highest)
        return highest

    # --- metadata validation ---------------------------------------------

    @classmethod
    def _validate_metadata(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate the metadata envelope version, failing closed.

        Step 11 stops migrating legacy payloads in memory. A v1/v2 envelope
        describes a plain ``IndexFlatIP`` whose rows carry no native IDs, so its
        ID map cannot be verified against the index; it is rejected with rebuild
        guidance instead of being stamped with the current version.

        This is the envelope check shared with ``knowcode doctor``; the
        engine, dimension, and live-count checks need the native index and live
        in :meth:`load`.
        """
        schema_version = data.get("schema_version")
        if schema_version is None:
            raise cls._artifact_error(
                "vector metadata has no schema_version; it predates the versioned "
                "vector contract"
            )

        try:
            normalized = cls._normalize_schema_version(schema_version)
        except ValueError as exc:
            raise cls._artifact_error(str(exc)) from exc

        if normalized not in cls.SUPPORTED_SCHEMA_VERSIONS:
            raise cls._artifact_error(
                f"unsupported vector index schema version {schema_version!r}; "
                f"supported versions: {sorted(cls.SUPPORTED_SCHEMA_VERSIONS)}"
            )

        validated = dict(data)
        validated["schema_version"] = normalized
        return validated

    @classmethod
    def _artifact_error(cls, message: str) -> VectorArtifactVersionError:
        """Build a fail-closed artifact error carrying rebuild guidance."""
        return VectorArtifactVersionError(f"{message}. {cls.REBUILD_HINT}")

    @staticmethod
    def _normalize_schema_version(value: Any) -> int:
        """Normalize schema version values represented as int/str."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        raise ValueError(f"invalid vector schema version value: {value!r}")
