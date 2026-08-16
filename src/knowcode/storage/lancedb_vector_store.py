"""LanceDB-backed vector store for disk-based retrieval.

Step 12 (ADR 7) repairs the reviewed injection and buffer-synchronization
defects. Chunk IDs used to be interpolated straight into LanceDB's SQL-like
predicates (``where(f"id = '{chunk_id}'")``, ``delete(f"id = '{chunk_id}'")``),
so the repository-derived ID ``x' OR true --`` widened a read to another row and
deleted every row in the table.

The locked LanceDB version accepts only string predicates for ``delete`` and
``where``, so exactness cannot come from a typed expression API. It comes from
the column the predicate names instead: every row carries ``key``, the SHA-256
hex digest of its chunk ID, and every predicate this store issues is built by
:meth:`LanceDBVectorStore._key_predicate` from that fixed-width hex digest. A
digest is structurally incapable of carrying quotes, operators, or comments, so
no repository ID can reach the filter grammar — this is not quote escaping, and
it is not a filename whitelist, so every legal path keeps working. The chunk ID
itself is verified in Python against the returned row, so matching is exact on
the data rather than on the predicate.

The mutable write buffer is now synchronized with the durable table under one
lock, and the metadata envelope is schema 2 with dimension, live count, and
generation, validated on load.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Sequence

from knowcode.errors import (
    VectorArtifactVersionError,
    VectorContractError,
    VectorDimensionError,
)
from knowcode.utils.atomic_write import atomic_write_json
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

try:
    import lancedb
except ImportError:  # pragma: no cover - exercised only without the extra
    lancedb = None

try:
    import pyarrow as pa
except ImportError:  # pragma: no cover - pyarrow ships with lancedb
    pa = None  # type: ignore[assignment]

#: A key literal may contain nothing but lowercase hex, which is what makes the
#: predicate grammar unreachable from repository data.
_HEX_KEY = re.compile(r"\A[0-9a-f]{64}\Z")


class LanceDBVectorStore:
    """LanceDB-based vector store for persistent disk-based retrieval.

    Locking and visibility follow the Step 10 contract: ``add``, ``upsert``,
    ``remove``, ``clear``, ``flush``, ``save``, and ``load`` are serialized
    mutations, and ``search``, ``get_embedding``, and ``count`` flush first so a
    write that has returned is always observable by a later read.

    Chunk IDs are exact-match data: each maps to one live row, so adding an ID
    that already exists replaces its single row rather than duplicating it.
    """

    TABLE_NAME = "vectors"
    #: Metadata envelope version. Bumped to 2 by Step 12: the table now carries
    #: an exact-key column and the envelope carries live count and generation,
    #: none of which a v1 artifact can supply.
    SCHEMA_VERSION = 2
    SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
    BACKEND = "lancedb"
    REBUILD_HINT = "Rebuild the semantic index with `knowcode build`."

    KEY_COLUMN = "key"
    ID_COLUMN = "id"
    VECTOR_COLUMN = "vector"

    #: Buffered rows are drained into the table once this many accumulate.
    BUFFER_LIMIT = 1000
    #: Pending deletes are issued in batches so one predicate stays bounded.
    DELETE_BATCH_SIZE = 128

    def __init__(self, dimension: int, path: str | Path | None = None) -> None:
        """Initialize LanceDB vector store.

        Args:
            dimension: Expected embedding dimensionality.
            path: Directory path for LanceDB. If None, uses in-memory DB.

        Raises:
            VectorArtifactVersionError: When ``path`` holds a table written
                before the exact-key schema, or one whose vectors disagree with
                ``dimension``.
        """
        self._require_lancedb()
        self.dimension = dimension
        self.path = str(path) if path else "memory://"
        self.db = lancedb.connect(self.path)
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False

        # Buffered inserts keyed by chunk ID, so a replacement that has not
        # reached the table yet overwrites in place instead of duplicating.
        self._buffer: dict[str, list[float]] = {}
        # Chunk IDs whose *durable* row is stale and must be deleted at flush.
        self._pending_deletes: set[str] = set()
        # Chunk IDs the table currently holds.
        self._durable_ids: set[str] = set()
        # digest -> chunk ID for every live row, buffered or durable.
        self._live_keys: dict[str, str] = {}

        self._table: Any = None
        if self._table_exists(self.db):
            table = self.db.open_table(self.TABLE_NAME)
            self._require_table_schema(table, self.dimension, str(self.path))
            self._adopt_table(table)

    # --- exact-key predicates ---------------------------------------------

    @classmethod
    def _digest(cls, chunk_id: str) -> str:
        """Return the hex digest that identifies ``chunk_id`` in a predicate."""
        return hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()

    @classmethod
    def _key_literal(cls, key: str) -> str:
        """Quote a digest for a predicate, proving it is hex before it is used.

        This assertion is the boundary the injection defect crossed. It can only
        fail on a programming error (a caller passing something other than a
        digest), never on repository data, because every caller passes
        :meth:`_digest` output.
        """
        if not _HEX_KEY.match(key):
            raise VectorContractError(
                f"Refusing to build a LanceDB predicate from a non-digest key: {key!r}."
            )
        return f"'{key}'"

    @classmethod
    def _key_predicate(cls, chunk_id: str) -> str:
        """Build the exact-match predicate for one chunk ID."""
        return f"{cls.KEY_COLUMN} = {cls._key_literal(cls._digest(chunk_id))}"

    @classmethod
    def _keys_predicate(cls, keys: Sequence[str]) -> str:
        """Build the exact-match predicate for a batch of digests."""
        literals = ", ".join(cls._key_literal(key) for key in keys)
        return f"{cls.KEY_COLUMN} IN ({literals})"

    # --- mutation ---------------------------------------------------------

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding, replacing any existing row for that ID.

        Add is exact-ID add-or-replace so re-indexing a chunk cannot leave two
        rows that both occupy result slots and bias fusion scores.
        """
        self._put(chunk_id, embedding)

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        """Exact-ID idempotent add-or-replace."""
        self._put(chunk_id, embedding)

    def _put(self, chunk_id: str, embedding: list[float]) -> None:
        """Replace ``chunk_id``'s row with ``embedding`` under the lock."""
        self._require_dimension(embedding)
        key = self._digest(chunk_id)
        with self._lock:
            self._require_no_collision(key, chunk_id)
            self._buffer[chunk_id] = list(embedding)
            if chunk_id in self._durable_ids:
                self._pending_deletes.add(chunk_id)
            self._live_keys[key] = chunk_id
            self._generation += 1
            if len(self._buffer) >= self.BUFFER_LIMIT:
                self._flush_locked()

    def remove(self, chunk_id: str) -> None:
        """Remove a chunk from the index by its exact ID.

        Removing an absent ID is a no-op, and a buffered row is dropped without
        ever reaching the table.
        """
        key = self._digest(chunk_id)
        with self._lock:
            if self._live_keys.get(key) != chunk_id:
                return
            del self._live_keys[key]
            self._buffer.pop(chunk_id, None)
            if chunk_id in self._durable_ids:
                self._pending_deletes.add(chunk_id)
            self._generation += 1

    def clear(self) -> None:
        """Drop the table and reset all buffered and durable bookkeeping."""
        with self._lock:
            self._buffer = {}
            self._pending_deletes = set()
            self._durable_ids = set()
            self._live_keys = {}
            if self._table is not None:
                self.db.drop_table(self.TABLE_NAME)
                self._table = None
            self._generation += 1

    def flush(self) -> None:
        """Drain buffered writes and pending deletes into the durable table."""
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        """Drain the buffer and release the database handle. Idempotent (Step 18).

        A retired generation's store is closed only after its last reader
        releases, so this runs with nobody reading. The buffer is drained first
        because a write that already returned must not be lost by the close
        that follows it — this backend's durability boundary is the table, and
        an un-drained buffer would silently discard committed work.

        A closed store is empty rather than poisoned, matching
        :class:`~knowcode.storage.vector_store.VectorStore`: the retiring
        service has stopped routing readers here, and a late ``count()`` from
        shutdown diagnostics should report zero rather than raise.
        """
        with self._lock:
            if self._closed:
                return
            try:
                self._flush_locked()
            except Exception:  # noqa: BLE001 - reported; the handle still closes
                logger.exception(
                    "Draining the LanceDB buffer during close failed; "
                    "releasing the handle anyway"
                )
            self._closed = True
            self._table = None
            self.db = None
            self._buffer = {}
            self._pending_deletes = set()
            self._durable_ids = set()
            self._live_keys = {}
            self._generation += 1

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`close` has released this store's handle."""
        with self._lock:
            return self._closed

    def _flush_locked(self) -> None:
        """Apply pending deletes, then buffered inserts, in that order.

        Deletes run first because a replacement's stale durable row must go
        before its new buffered row lands, otherwise the ID would briefly hold
        two rows and the delete would remove both.
        """
        if self._pending_deletes:
            if self._table is not None:
                keys = [self._digest(chunk_id) for chunk_id in self._pending_deletes]
                for start in range(0, len(keys), self.DELETE_BATCH_SIZE):
                    batch = keys[start : start + self.DELETE_BATCH_SIZE]
                    self._table.delete(self._keys_predicate(batch))
            self._durable_ids -= self._pending_deletes
            self._pending_deletes = set()

        if self._buffer:
            rows = [
                {
                    self.KEY_COLUMN: self._digest(chunk_id),
                    self.ID_COLUMN: chunk_id,
                    self.VECTOR_COLUMN: embedding,
                }
                for chunk_id, embedding in self._buffer.items()
            ]
            if self._table is None:
                self._table = self.db.create_table(
                    self.TABLE_NAME,
                    data=rows,
                    schema=self._arrow_schema(self.dimension),
                )
            else:
                self._table.add(rows)
            self._durable_ids |= set(self._buffer)
            self._buffer = {}

    def _require_dimension(self, embedding: list[float]) -> None:
        """Raise VectorDimensionError when the embedding length disagrees.

        LanceDB otherwise raises a low-level Arrow ValueError at *flush* time
        on an unrelated later call, so validate eagerly at add/upsert time.
        """
        actual = len(embedding)
        if actual != self.dimension:
            raise VectorDimensionError(self.dimension, actual)

    def _require_no_collision(self, key: str, chunk_id: str) -> None:
        """Reject the (astronomically unlikely) digest collision explicitly.

        Two live chunk IDs sharing one digest would make a key predicate match
        both rows, which is the one way exactness could still be lost. It is
        reported rather than silently widening a read or delete.
        """
        existing = self._live_keys.get(key)
        if existing is not None and existing != chunk_id:
            raise VectorContractError(
                f"Chunk IDs {existing!r} and {chunk_id!r} share one exact-match key; "
                "the vector index cannot address them separately."
            )

    # --- reads ------------------------------------------------------------

    def search(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """Search for similar embeddings.

        Returns:
            List of (chunk_id, similarity_score) tuples with unique chunk IDs.
        """
        with self._lock:
            self._flush_locked()
            if self._table is None or limit <= 0 or not self._live_keys:
                return []

            # LanceDB cosine distance is 1 - cosine similarity.
            results = (
                self._table.search(embedding).metric("cosine").limit(limit).to_list()
            )
            return [
                (str(row[self.ID_COLUMN]), 1.0 - row["_distance"]) for row in results
            ]

    def get_embedding(self, chunk_id: str) -> list[float] | None:
        """Fetch the embedding for a chunk ID, or None when it is absent."""
        with self._lock:
            self._flush_locked()
            if self._table is None:
                return None
            if self._live_keys.get(self._digest(chunk_id)) != chunk_id:
                return None

            rows = (
                self._table.search(None)
                .where(self._key_predicate(chunk_id))
                .select([self.ID_COLUMN, self.VECTOR_COLUMN])
                .limit(1)
                .to_list()
            )
            for row in rows:
                # Exactness is verified on the returned data, never assumed
                # from the predicate.
                if row[self.ID_COLUMN] == chunk_id:
                    return [float(value) for value in row[self.VECTOR_COLUMN]]
            return None

    def count(self) -> int:
        """Return the number of live vectors in the index."""
        with self._lock:
            self._flush_locked()
            return len(self._live_keys)

    def ids(self) -> list[str]:
        """Return every live chunk ID, sorted.

        Used by the vector contract tests and by generation parity checks that
        need chunk/vector membership rather than a count alone.
        """
        with self._lock:
            self._flush_locked()
            return sorted(self._live_keys.values())

    # --- persistence ------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist the LanceDB table and the metadata envelope to disk."""
        path = Path(path)
        with self._lock:
            self._flush_locked()
            path.parent.mkdir(parents=True, exist_ok=True)
            target = path.with_suffix(".lancedb")
            if not self._lives_at(target):
                self._export_locked(target)

            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "backend": self.BACKEND,
                "dimension": self.dimension,
                "count": len(self._live_keys),
                "generation": self._generation,
            }
            # Metadata last: the envelope declares the table's dimension and
            # live count, so it must never be published before the table it
            # describes (Step 13 publication ordering).
            atomic_write_json(path.with_suffix(".json"), payload)

    def _lives_at(self, target: Path) -> bool:
        """Whether this store already writes directly into ``target``."""
        if self.path.startswith("memory://"):
            return False
        return Path(self.path).resolve() == target.resolve()

    def _export_locked(self, target: Path) -> None:
        """Write the live rows into ``target`` as a self-contained database.

        A directory-backed store is copied wholesale; an in-memory store is
        replayed into a fresh database so ``save``/``load`` are symmetric on
        every configuration rather than silently dropping in-memory rows.
        """
        if target.exists():
            shutil.rmtree(target)
        if not self.path.startswith("memory://"):
            shutil.copytree(self.path, target)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(target))
        schema = self._arrow_schema(self.dimension)
        rows = self._read_rows_locked()
        if rows:
            db.create_table(self.TABLE_NAME, data=rows, schema=schema)
        else:
            db.create_table(self.TABLE_NAME, schema=schema)

    def _read_rows_locked(self) -> list[dict[str, Any]]:
        """Read every live row out of the table (buffer already flushed)."""
        if self._table is None:
            return []
        total = int(self._table.count_rows())
        if total == 0:
            return []
        return [
            {
                self.KEY_COLUMN: row[self.KEY_COLUMN],
                self.ID_COLUMN: row[self.ID_COLUMN],
                self.VECTOR_COLUMN: [float(value) for value in row[self.VECTOR_COLUMN]],
            }
            for row in self._table.search(None)
            .select([self.KEY_COLUMN, self.ID_COLUMN, self.VECTOR_COLUMN])
            .limit(total)
            .to_list()
        ]

    def load(self, path: Path) -> None:
        """Load and validate a persisted generation, failing closed on drift.

        A path with no vector artifacts at all loads as an empty index, which is
        how a fresh :class:`~knowcode.indexing.indexer.Indexer` reads a
        directory that has never been saved. Anything else — a half-published
        generation, a legacy v1 envelope, a foreign backend, or a table that
        disagrees with the envelope — is rejected with rebuild guidance.
        """
        self._require_lancedb()
        path = Path(path)
        target = path.with_suffix(".lancedb")
        json_file = path.with_suffix(".json")

        with self._lock:
            if not json_file.exists():
                # The directory alone proves nothing: ``lancedb.connect`` creates
                # it, so a store merely pointed at this index has one with no
                # table inside. Only a persisted table means "saved".
                if not self._has_persisted_table(target):
                    return  # never saved: an empty index is the correct state
                raise self._artifact_error(
                    f"vector index at {path} has no metadata envelope "
                    f"({json_file.name} is missing)"
                )

            try:
                with open(json_file, encoding="utf-8") as file:
                    data = json.load(file)
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
            self._require_backend(metadata, json_file)
            dimension = self._declared_dimension(metadata, json_file)

            if not target.is_dir():
                raise self._artifact_error(
                    f"vector metadata {json_file.name} has no {target.name} artifact"
                )

            # Prepare the whole candidate generation before touching live state:
            # a rejected artifact must leave this store exactly as it was.
            db = lancedb.connect(str(target))
            if not self._table_exists(db):
                raise self._artifact_error(
                    f"vector artifact {target.name} has no {self.TABLE_NAME!r} table"
                )
            table = db.open_table(self.TABLE_NAME)
            self._require_table_schema(table, dimension, str(json_file))
            live_keys = self._read_live_keys(table, json_file)
            self._require_declared_count(metadata, live_keys, json_file)

            self.path = str(target)
            self.db = db
            self.dimension = dimension
            self._buffer = {}
            self._pending_deletes = set()
            self._adopt_table(table, live_keys=live_keys)
            self._generation = self._declared_generation(metadata)

    def _has_persisted_table(self, target: Path) -> bool:
        """Whether ``target`` is a database that actually holds the table."""
        if not target.is_dir():
            return False
        return self._table_exists(lancedb.connect(str(target)))

    def _table_exists(self, db: Any) -> bool:
        if hasattr(db, "list_tables"):
            res = db.list_tables()
            if hasattr(res, "tables"):
                return self.TABLE_NAME in res.tables
        return self.TABLE_NAME in db.table_names()

    def _adopt_table(
        self, table: Any, *, live_keys: dict[str, str] | None = None
    ) -> None:
        """Make ``table`` the live table and rebuild its ID bookkeeping."""
        self._table = table
        self._live_keys = (
            live_keys if live_keys is not None else self._read_live_keys(table, None)
        )
        self._durable_ids = set(self._live_keys.values())

    def _read_live_keys(self, table: Any, source: Path | None) -> dict[str, str]:
        """Read the table's chunk IDs, rejecting duplicates and collisions."""
        total = int(table.count_rows())
        if total == 0:
            return {}
        rows = table.search(None).select([self.ID_COLUMN]).limit(total).to_list()

        live_keys: dict[str, str] = {}
        for row in rows:
            chunk_id = str(row[self.ID_COLUMN])
            key = self._digest(chunk_id)
            existing = live_keys.get(key)
            if existing == chunk_id:
                raise self._artifact_error(
                    f"vector table holds more than one row for chunk ID {chunk_id!r}"
                    + (f" ({source.name})" if source is not None else "")
                )
            if existing is not None:
                raise self._artifact_error(
                    f"chunk IDs {existing!r} and {chunk_id!r} share one exact-match key"
                )
            live_keys[key] = chunk_id
        return live_keys

    # --- artifact validation ----------------------------------------------

    @classmethod
    def _arrow_schema(cls, dimension: int) -> Any:
        """Arrow schema for the vectors table, with a fixed-width vector."""
        if pa is None:  # pragma: no cover - pyarrow ships with lancedb
            raise ImportError(
                "pyarrow is not installed. Install with 'pip install knowcode[search]'"
            )
        return pa.schema(
            [
                pa.field(cls.KEY_COLUMN, pa.string(), nullable=False),
                pa.field(cls.ID_COLUMN, pa.string(), nullable=False),
                pa.field(
                    cls.VECTOR_COLUMN, pa.list_(pa.float32(), dimension), nullable=False
                ),
            ]
        )

    @classmethod
    def _require_table_schema(cls, table: Any, dimension: int, source: str) -> None:
        """Reject a table without the exact-key column or the right width."""
        names = set(table.schema.names)
        missing = {cls.KEY_COLUMN, cls.ID_COLUMN, cls.VECTOR_COLUMN} - names
        if missing:
            raise cls._artifact_error(
                f"vector table at {source} is missing column(s) "
                f"{sorted(missing)}; it predates the exact-key schema"
            )
        actual = getattr(table.schema.field(cls.VECTOR_COLUMN).type, "list_size", None)
        if actual != dimension:
            raise cls._artifact_error(
                f"vector table at {source} holds {actual}-dimensional vectors but "
                f"{dimension} was expected"
            )

    @classmethod
    def _validate_metadata(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate the metadata envelope version, failing closed.

        A v1 envelope describes a table with no exact-key column, whose rows may
        also carry duplicate IDs or the residue of an injected delete. It is
        rejected with rebuild guidance rather than stamped with the current
        version. This is the envelope check shared with ``knowcode doctor``;
        checks that need the table itself live in :meth:`load`.
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
    def _require_backend(cls, metadata: dict[str, Any], source: Path) -> None:
        """Reject an artifact written by another backend."""
        recorded = metadata.get("backend")
        if recorded != cls.BACKEND:
            raise cls._artifact_error(
                f"vector artifact {source.name} declares backend {recorded!r} but "
                f"this store is {cls.BACKEND!r}"
            )

    @classmethod
    def _declared_dimension(cls, metadata: dict[str, Any], source: Path) -> int:
        """Return the envelope's dimension once it is a usable integer."""
        recorded = metadata.get("dimension")
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            raise cls._artifact_error(
                f"invalid vector metadata in {source}: 'dimension' must be an integer"
            )
        return recorded

    @classmethod
    def _require_declared_count(
        cls, metadata: dict[str, Any], live_keys: dict[str, str], source: Path
    ) -> None:
        """Reject an envelope whose count disagrees with the table."""
        recorded = metadata.get("count")
        if recorded is not None and recorded != len(live_keys):
            raise cls._artifact_error(
                f"vector metadata {source.name} declares count {recorded} but its "
                f"table holds {len(live_keys)} vectors"
            )

    @staticmethod
    def _declared_generation(metadata: dict[str, Any]) -> int:
        """Return the envelope's generation, defaulting to zero."""
        recorded = metadata.get("generation")
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return recorded
        return 0

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

    @staticmethod
    def _require_lancedb() -> None:
        """Fail with install guidance when the optional extra is absent."""
        if lancedb is None:
            raise ImportError(
                "lancedb is not installed. Install with 'pip install lancedb' "
                "or 'pip install knowcode[search]'"
            )
