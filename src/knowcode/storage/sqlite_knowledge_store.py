"""SQLite-backed knowledge store.

Connection ownership (hardening Step 09, ADR 2): one serialized writer
connection (``_writer_conn``) guarded by ``_write_lock`` and many thread-local
reader connections. A reader always uses a connection separate from the writer,
opened in autocommit with ``query_only=ON``, so it observes a committed WAL
snapshot and can never see an in-flight writer transaction.

A public :meth:`bulk_insert` owns its connection, lock, transaction, and
rollback for the whole batch; callers (``service.analyze`` and ``from_json``)
no longer drive a manual ``BEGIN``/``COMMIT`` around individually locking
methods, which previously interleaved on the shared connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.errors import RepositoryClosedError
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.utils.entity_identity import (
    absolutize_id,
    ensure_entity_content_hash,
    normalize_file_identity,
    pack_content_hash,
    relativize_id,
    unpack_content_hash,
)


# An edge endpoint resolved to its codebook key. Endpoints are not always
# entities: an import lands on an ``external::`` id and an unresolved call on
# an ``unresolved::`` one, and neither has a row in ``entities`` to key on.
_EID_OF = "(SELECT id FROM eid WHERE entity_id = ?)"
_KIND_OF = "(SELECT id FROM relkind WHERE kind = ?)"

# Edges rendered back under the column names ``_row_to_relationship`` reads.
_EDGE_AS_TEXT = """
    SELECT s.entity_id AS source_id,
           t.entity_id AS target_id,
           k.kind      AS kind,
           r.metadata_json AS metadata_json
    FROM relationships r
    JOIN eid s     ON s.id = r.source_id
    JOIN eid t     ON t.id = r.target_id
    JOIN relkind k ON k.id = r.kind
"""

_ALL_EDGES_SQL = _EDGE_AS_TEXT
_OUTGOING_EDGES_SQL = f"{_EDGE_AS_TEXT} WHERE r.source_id = {_EID_OF}"
_INCOMING_EDGES_SQL = f"{_EDGE_AS_TEXT} WHERE r.target_id = {_EID_OF}"

# Walk one edge and hydrate the entity at its far end. The joined column names
# the endpoint being walked to, so ``source_id`` returns an edge's sources.
_SOURCES_OF = f"""
    SELECT {{distinct}} e.* FROM entities e
    JOIN eid ne ON ne.entity_id = e.entity_id
    JOIN relationships r ON r.source_id = ne.id
    WHERE r.target_id = {_EID_OF} AND r.kind {{kinds}}
"""
_TARGETS_OF = f"""
    SELECT {{distinct}} e.* FROM entities e
    JOIN eid ne ON ne.entity_id = e.entity_id
    JOIN relationships r ON r.target_id = ne.id
    WHERE r.source_id = {_EID_OF} AND r.kind {{kinds}}
"""

_ONE_KIND = f"= {_KIND_OF}"
_TWO_KINDS = f"IN ({_KIND_OF}, {_KIND_OF})"

_CALLERS_SQL = _SOURCES_OF.format(distinct="", kinds=_ONE_KIND)
_CALLEES_SQL = _TARGETS_OF.format(distinct="", kinds=_ONE_KIND)
_PARENT_SQL = _SOURCES_OF.format(distinct="", kinds=_ONE_KIND)
_CHILDREN_SQL = _TARGETS_OF.format(distinct="", kinds=_ONE_KIND)
_DEPENDENCIES_SQL = _TARGETS_OF.format(distinct="DISTINCT", kinds=_TWO_KINDS)
_DEPENDENTS_SQL = _SOURCES_OF.format(distinct="DISTINCT", kinds=_TWO_KINDS)

_IMPORTS_SQL = f"""
    SELECT t.entity_id AS target_id FROM relationships r
    JOIN eid t ON t.id = r.target_id
    WHERE r.source_id = {_EID_OF} AND r.kind = {_KIND_OF}
"""


class SqliteKnowledgeStore:
    """SQLite-backed knowledge store with recursive query support."""

    SCHEMA_VERSION = 1

    # Ids are stored relative to this root and hydrated back to absolute on
    # the way out, so a generation's size does not depend on where it was
    # built (ADR 1, ADR 10). Empty until set_repo_root binds it.
    _repo_root = ""

    def __init__(
        self, db_path: str | Path, *, persist_entity_source: bool = True
    ) -> None:
        """Initialize SQLite knowledge store.

        Args:
            db_path: Path to the ``.db`` file. Created if it does not exist.
            persist_entity_source: When False (storage plan D3, config
                ``entity_source: disk``), ``entities.source_code`` is written
                as NULL and readers resolve the text from the working tree
                against the stored content hash. The column stays in the
                schema either way, so the setting flips per build without a
                migration.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_entity_source = persist_entity_source

        # Serialized writer connection (ADR 2). Deferred mode (isolation_level="")
        # so `with self._writer_conn:` issues a real BEGIN/commit/rollback. This
        # is a change from the previous autocommit (isolation_level=None) writer,
        # where `with conn:` would have been a transaction no-op.
        self._write_lock = threading.Lock()
        self._writer_conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._writer_conn.row_factory = sqlite3.Row

        # Read gate: counter + condition so close() can drain in-flight readers
        # before tearing connections down. Lock order is _write_lock ->
        # _read_gate, never the reverse.
        self._read_gate_cond = threading.Condition()
        self._active_readers = 0
        self._closing = False
        self._closed = False

        # Thread-local reader connections, tracked for deterministic close.
        self._thread_local = threading.local()
        self._reader_conns: set[sqlite3.Connection] = set()
        self._reader_conns_lock = threading.Lock()
        self._reader_epoch = 0

        self._init_schema()

    # ------------------------------------------------------------------
    # Connection ownership (ADR 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_writer_pragmas(conn: sqlite3.Connection) -> None:
        """Set durability/locking PRAGMAs on a writer connection."""
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    def _open_reader_conn(self) -> sqlite3.Connection:
        """Open a new autocommit reader connection for the current path."""
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=True,
            isolation_level=None,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _reader_conn(self) -> sqlite3.Connection:
        """Return the calling thread's reader connection, creating it lazily."""
        tls = self._thread_local
        cached: sqlite3.Connection | None = getattr(tls, "conn", None)
        if cached is not None and getattr(tls, "epoch", -1) == self._reader_epoch:
            return cached
        if cached is not None:
            try:
                cached.close()
            except sqlite3.Error:
                pass
        conn = self._open_reader_conn()
        tls.conn = conn
        tls.epoch = self._reader_epoch
        with self._reader_conns_lock:
            self._reader_conns.add(conn)
        return conn

    @contextmanager
    def _read_lease(self) -> Iterator[sqlite3.Connection]:
        """Acquire a reader lease for the duration of one read operation.

        Mirrors :meth:`SqliteChunkRepository._read_lease`: entering tests
        ``_closing``/``_closed`` and bumps ``_active_readers`` atomically;
        ``close()`` waits here until readers drain before tearing connections
        down.
        """
        with self._read_gate_cond:
            if self._closing or self._closed:
                raise RepositoryClosedError(
                    "SqliteKnowledgeStore is closed; open a new instance."
                )
            self._active_readers += 1
        try:
            yield self._reader_conn()
        finally:
            with self._read_gate_cond:
                self._active_readers -= 1
                if self._active_readers == 0:
                    self._read_gate_cond.notify_all()

    def _drain_and_close_readers(self) -> None:
        """Close every tracked reader connection and invalidate caches."""
        with self._reader_conns_lock:
            conns = list(self._reader_conns)
            self._reader_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._reader_epoch += 1

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Initialize database schema.

        WAL is a per-database property and is set outside any transaction.
        Schema creation runs inside one deferred writer transaction.
        """
        conn = self._writer_conn
        conn.execute("PRAGMA journal_mode=WAL")
        self._apply_writer_pragmas(conn)
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_start INT NOT NULL,
                    line_end INT NOT NULL,
                    docstring TEXT,
                    signature TEXT,
                    source_code TEXT,
                    metadata_json TEXT,
                    content_hash BLOB
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_root (
                    root TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eid (
                    id INTEGER PRIMARY KEY,
                    entity_id TEXT UNIQUE NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relkind (
                    id INTEGER PRIMARY KEY,
                    kind TEXT UNIQUE NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationships (
                    source_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    kind INTEGER NOT NULL,
                    metadata_json TEXT
                )
                """
            )
            # Indexes
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_qualified_name ON entities(qualified_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relationships_source_kind "
                "ON relationships(source_id, kind)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relationships_target_kind "
                "ON relationships(target_id, kind)"
            )
            self._reject_string_keyed_edges(conn)
        self._load_repo_root()

    def _load_repo_root(self) -> None:
        """Adopt the root already recorded in this database, if any."""
        row = self._writer_conn.execute("SELECT root FROM repo_root").fetchone()
        self._repo_root = row[0] if row else ""

    def set_repo_root(self, root: str | Path) -> None:
        """Bind this database to the repository root its ids are anchored at.

        Rows written before the bind keep their absolute ids, which read back
        unchanged: the codec strips only what it can re-add. Rebinding a
        populated database to a different root would silently re-anchor those
        rows, so it is refused, matching ADR 1's rule that a moved repository
        is rebuilt rather than rewritten.
        """
        normalized = normalize_file_identity(root)
        with self._write_lock:
            current = self._writer_conn.execute("SELECT root FROM repo_root").fetchone()
            if current and current[0] != normalized:
                raise ValueError(
                    f"{self._db_path} is already bound to repository root "
                    f"{current[0]!r}; rebuild the index to anchor it at "
                    f"{normalized!r}."
                )
            if not current:
                with self._writer_conn:
                    self._writer_conn.execute(
                        "INSERT INTO repo_root (root) VALUES (?)", (normalized,)
                    )
            self._repo_root = normalized

    def _store_id(self, value: str) -> str:
        """Render a caller's absolute id in its stored, root-relative form."""
        return relativize_id(value, self._repo_root)

    def _load_id(self, value: str) -> str:
        """Render a stored id back as the absolute id callers hold."""
        return absolutize_id(value, self._repo_root)

    @staticmethod
    def _reject_string_keyed_edges(conn: sqlite3.Connection) -> None:
        """Fail closed on a knowledge.db whose edges still hold entity id text.

        ``CREATE TABLE IF NOT EXISTS`` leaves an older edge table in place, and
        integers written into it would read back as ids that match nothing.
        A populated edge table with no codebook entry is that database.
        """
        edges = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        if not edges:
            return
        if conn.execute("SELECT COUNT(*) FROM eid").fetchone()[0]:
            return
        raise ValueError(
            "knowledge.db holds string-keyed relationships and no endpoint "
            "codebook. Rebuild the index with `knowcode build`; edge keys "
            "cannot be migrated without the entity ids they were written from."
        )

    # ------------------------------------------------------------------
    # Mutation (writer connection, serialized by _write_lock)
    # ------------------------------------------------------------------

    def _insert_entity_row(self, conn: sqlite3.Connection, entity: Entity) -> None:
        """Insert one entity row on the caller's open transaction.

        Same SQL as :meth:`add_entity`. Private so :meth:`bulk_insert` can use
        it without re-acquiring the writer lock (which would deadlock).
        """
        stored_metadata = {
            key: value
            for key, value in entity.metadata.items()
            if key != "content_hash"
        }
        metadata_json = json.dumps(stored_metadata) if stored_metadata else "{}"
        conn.execute(
            """
            INSERT OR REPLACE INTO entities (
                entity_id, kind, name, qualified_name, file_path,
                line_start, line_end, docstring, signature,
                source_code, metadata_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._store_id(entity.id),
                entity.kind.value,
                entity.name,
                entity.qualified_name,
                self._store_id(entity.location.file_path),
                entity.location.line_start,
                entity.location.line_end,
                entity.docstring,
                entity.signature,
                # D3: the persisted copy is the configurable half. Nulling it
                # here — after the digest was computed upstream — keeps every
                # write path (build, watch, bulk) on one seam.
                entity.source_code if self._persist_entity_source else None,
                metadata_json,
                pack_content_hash(entity.metadata.get("content_hash")),
            ),
        )

    def _insert_relationship_row(
        self, conn: sqlite3.Connection, rel: Relationship
    ) -> None:
        """Insert one relationship row on the caller's open transaction.

        The endpoints and the kind are interned first, so the edge row itself
        holds three integers. An empty metadata payload is stored as NULL
        rather than ``{}``; :meth:`_row_to_relationship` reads both as ``{}``.
        """
        conn.execute(
            "INSERT OR IGNORE INTO eid (entity_id) VALUES (?), (?)",
            (self._store_id(rel.source_id), self._store_id(rel.target_id)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO relkind (kind) VALUES (?)", (rel.kind.value,)
        )
        conn.execute(
            f"""
            INSERT INTO relationships (source_id, target_id, kind, metadata_json)
            VALUES ({_EID_OF}, {_EID_OF}, {_KIND_OF}, ?)
            """,
            (
                self._store_id(rel.source_id),
                self._store_id(rel.target_id),
                rel.kind.value,
                json.dumps(rel.metadata) if rel.metadata else None,
            ),
        )

    def bulk_insert(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        """Insert all entities and relationships in one writer transaction.

        Owns the connection, lock, ``BEGIN``/commit, and rollback for the whole
        batch (ADR 2): callers must not wrap individually locking methods in an
        outer transaction. Uses the private row helpers rather than
        :meth:`add_entity`/:meth:`add_relationship`, which would re-acquire the
        writer lock and deadlock.
        """
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteKnowledgeStore is closed; open a new instance."
                )
            with self._writer_conn:
                self._commit_bulk(self._writer_conn, entities, relationships)

    def _commit_bulk(
        self,
        conn: sqlite3.Connection,
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        """Insert all rows inside the caller's open transaction (test seam).

        Factored out so fault injection can substitute this step to prove the
        batch rolls back atomically or to pause mid-transaction for a
        dirty-read assertion. It never opens its own transaction.
        """
        for entity in entities:
            ensure_entity_content_hash(entity)
            self._insert_entity_row(conn, entity)
        for rel in relationships:
            self._insert_relationship_row(conn, rel)

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the store."""
        ensure_entity_content_hash(entity)
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteKnowledgeStore is closed; open a new instance."
                )
            with self._writer_conn:
                self._insert_entity_row(self._writer_conn, entity)

    def add_relationship(self, rel: Relationship) -> None:
        """Add a relationship to the store."""
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteKnowledgeStore is closed; open a new instance."
                )
            with self._writer_conn:
                self._insert_relationship_row(self._writer_conn, rel)

    # ------------------------------------------------------------------
    # Aggregations (reader connection)
    # ------------------------------------------------------------------

    def count_by_kind(self) -> dict[str, dict[str, int]]:
        """Return entity and relationship counts grouped by kind.

        Replaces the previous O(n) full-table hydration that ``get_stats`` used
        to perform. The result is::

            {"entities": {<kind>: <count>, ...},
             "relationships": {<kind>: <count>, ...}}
        """
        entities: dict[str, int] = {}
        relationships: dict[str, int] = {}
        with self._read_lease() as conn:
            for row in conn.execute(
                "SELECT kind, COUNT(*) AS c FROM entities GROUP BY kind"
            ):
                entities[row["kind"]] = int(row["c"])
            for row in conn.execute(
                "SELECT k.kind AS kind, COUNT(*) AS c FROM relationships r "
                "JOIN relkind k ON k.id = r.kind GROUP BY k.kind"
            ):
                relationships[row["kind"]] = int(row["c"])
        return {"entities": entities, "relationships": relationships}

    # ------------------------------------------------------------------
    # Row hydration
    # ------------------------------------------------------------------

    def _row_to_entity(self, row: sqlite3.Row) -> Entity:
        """Convert a database row to an Entity."""
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        if row["content_hash"]:
            metadata["content_hash"] = unpack_content_hash(row["content_hash"])

        location = Location(
            file_path=self._load_id(row["file_path"]),
            line_start=row["line_start"],
            line_end=row["line_end"],
        )
        return Entity(
            id=self._load_id(row["entity_id"]),
            kind=EntityKind(row["kind"]),
            name=row["name"],
            qualified_name=row["qualified_name"],
            location=location,
            docstring=row["docstring"],
            signature=row["signature"],
            source_code=row["source_code"],
            metadata=metadata,
        )

    def _row_to_relationship(self, row: sqlite3.Row) -> Relationship:
        """Convert a database row to a Relationship."""
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        return Relationship(
            source_id=self._load_id(row["source_id"]),
            target_id=self._load_id(row["target_id"]),
            kind=RelationshipKind(row["kind"]),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Reads (reader connection via _read_lease)
    # ------------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Fetch an entity by ID."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
            )
            row = cursor.fetchone()
        return self._row_to_entity(row) if row else None

    def search(self, pattern: str) -> list[Entity]:
        """Search entities by name or qualified name pattern."""
        like_pattern = f"%{pattern}%"
        with self._read_lease() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM entities
                WHERE name LIKE ? OR qualified_name LIKE ?
                """,
                (like_pattern, like_pattern),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def get_callers(self, entity_id: str) -> list[Entity]:
        """Return caller entities."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _CALLERS_SQL,
                (entity_id, RelationshipKind.CALLS.value),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def get_callees(self, entity_id: str) -> list[Entity]:
        """Return callee entities."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _CALLEES_SQL,
                (entity_id, RelationshipKind.CALLS.value),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def get_dependencies(self, entity_id: str) -> list[Entity]:
        """Return entities this entity depends on."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _DEPENDENCIES_SQL,
                (
                    entity_id,
                    RelationshipKind.CALLS.value,
                    RelationshipKind.IMPORTS.value,
                ),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def get_dependents(self, entity_id: str) -> list[Entity]:
        """Return entities depending on this entity."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _DEPENDENTS_SQL,
                (
                    entity_id,
                    RelationshipKind.CALLS.value,
                    RelationshipKind.IMPORTS.value,
                ),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    @property
    def entities(self) -> dict[str, Entity]:
        """Get all entities as a dictionary mapping ID to Entity."""
        with self._read_lease() as conn:
            cursor = conn.execute("SELECT * FROM entities")
            rows = cursor.fetchall()
        loaded = [self._row_to_entity(row) for row in rows]
        return {entity.id: entity for entity in loaded}

    @property
    def relationships(self) -> list[Relationship]:
        """Get all relationships."""
        with self._read_lease() as conn:
            cursor = conn.execute(_ALL_EDGES_SQL)
            rows = cursor.fetchall()
        return [self._row_to_relationship(row) for row in rows]

    def get_parent(self, entity_id: str) -> Optional[Entity]:
        """Get the parent entity (container) of an entity."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _PARENT_SQL,
                (entity_id, RelationshipKind.CONTAINS.value),
            )
            row = cursor.fetchone()
        return self._row_to_entity(row) if row else None

    def get_children(self, entity_id: str) -> list[Entity]:
        """Get entities contained by the given entity."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _CHILDREN_SQL,
                (entity_id, RelationshipKind.CONTAINS.value),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def get_imports(self, entity_id: str) -> list[str]:
        """Get imports for a module entity."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _IMPORTS_SQL,
                (entity_id, RelationshipKind.IMPORTS.value),
            )
            rows = cursor.fetchall()
        return [self._load_id(row["target_id"]) for row in rows]

    def get_outgoing_relationships(self, entity_id: str) -> list[Relationship]:
        """Return relationships where the entity is the source."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _OUTGOING_EDGES_SQL,
                (entity_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_relationship(row) for row in rows]

    def get_incoming_relationships(self, entity_id: str) -> list[Relationship]:
        """Return relationships where the entity is the target."""
        entity_id = self._store_id(entity_id)
        with self._read_lease() as conn:
            cursor = conn.execute(
                _INCOMING_EDGES_SQL,
                (entity_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_relationship(row) for row in rows]

    def list_by_kind(self, kind: EntityKind | str) -> list[Entity]:
        """List all entities of a given kind."""
        return self.get_entities_by_kind(kind)

    def get_entities_by_kind(self, kind: EntityKind | str) -> list[Entity]:
        """Return entities filtered by kind."""
        kind_str = kind.value if isinstance(kind, EntityKind) else kind
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT * FROM entities WHERE kind = ?",
                (kind_str,),
            )
            rows = cursor.fetchall()
        return [self._row_to_entity(row) for row in rows]

    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Multi-hop call graph traversal using Recursive CTE."""
        entity_id = self._store_id(entity_id)
        if direction not in ("callers", "callees"):
            raise ValueError(
                f"direction must be 'callers' or 'callees', got {direction}"
            )

        if direction == "callees":
            start_col, next_col = "source_id", "target_id"
        else:
            start_col, next_col = "target_id", "source_id"

        query = f"""
            WITH RECURSIVE call_chain(endpoint, depth) AS (
                SELECT {next_col}, 1 FROM relationships
                WHERE {start_col} = {_EID_OF} AND kind = {_KIND_OF}
              UNION ALL
                SELECT r.{next_col}, cc.depth + 1
                FROM relationships r
                JOIN call_chain cc ON r.{start_col} = cc.endpoint
                WHERE r.kind = {_KIND_OF} AND cc.depth < ?
            )
            SELECT DISTINCT e.entity_id, e.name, e.qualified_name, e.kind,
                   e.file_path, e.line_start, MIN(cc.depth) as call_depth
            FROM call_chain cc
            JOIN eid x ON x.id = cc.endpoint
            JOIN entities e ON e.entity_id = x.entity_id
            GROUP BY e.entity_id
            ORDER BY call_depth, e.name
            LIMIT ?;
        """

        with self._read_lease() as conn:
            cursor = conn.execute(
                query,
                (
                    entity_id,
                    RelationshipKind.CALLS.value,
                    RelationshipKind.CALLS.value,
                    depth,
                    max_results,
                ),
            )
            results = []
            for row in cursor:
                results.append(
                    {
                        "entity_id": self._load_id(row["entity_id"]),
                        "name": row["name"],
                        "qualified_name": row["qualified_name"],
                        "kind": row["kind"],
                        "file": row["file_path"],
                        "line": row["line_start"],
                        "call_depth": row["call_depth"],
                    }
                )
        return results

    def get_impact(self, entity_id: str, max_depth: int = 3) -> dict[str, Any]:
        """Analyze the impact of modifying or deleting an entity."""
        entity = self.get_entity(entity_id)
        if not entity:
            return {
                "entity_id": entity_id,
                "direct_dependents": [],
                "transitive_dependents": [],
                "risk_score": 0.0,
                "error": "Entity not found",
            }

        direct = self.trace_calls(
            entity_id, direction="callers", depth=1, max_results=100
        )
        transitive = self.trace_calls(
            entity_id, direction="callers", depth=max_depth, max_results=100
        )

        direct_ids = {d["entity_id"] for d in direct}
        transitive_only = [t for t in transitive if t["entity_id"] not in direct_ids]

        direct_count = len(direct)
        transitive_count = len(transitive_only)

        breadth_score = min(1.0, (direct_count + transitive_count * 0.5) / 20)

        affected_files = {d.get("file") for d in direct + transitive_only}
        file_score = min(1.0, len(affected_files) / 5)

        type_score = 0.3
        if entity.kind == EntityKind.CLASS:
            type_score = 0.6
        elif entity.kind == EntityKind.MODULE:
            type_score = 0.8

        risk_score = round((breadth_score + file_score + type_score) / 3, 2)

        return {
            "entity_id": entity_id,
            "entity_name": entity.qualified_name,
            "direct_dependents": direct,
            "transitive_dependents": transitive_only,
            "total_affected": direct_count + transitive_count,
            "affected_files": list(affected_files),
            "risk_score": min(1.0, risk_score),
        }

    # ------------------------------------------------------------------
    # Lifecycle and migration
    # ------------------------------------------------------------------

    def compact(self) -> None:
        """Rewrite the database into the fewest pages its rows need.

        Called once on a staged ``knowledge.db``, after the graph is written
        and before the generation is digested. Bulk-inserting entities and
        relationships leaves the index B-trees packed loosely, and that slack
        is copied into every retained generation until it is squeezed out here.

        Mirrors :meth:`SqliteChunkRepository.compact`, including its ordering
        constraint: a rewrite after the manifest is built breaks every digest.
        """
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteKnowledgeStore is closed; open a new instance."
                )
            # VACUUM refuses to run inside a transaction. Every write path here
            # commits its own, so this only settles a stray one.
            self._writer_conn.commit()
            self._writer_conn.execute("VACUUM")

    def close(self) -> None:
        """Close all connections idempotently after draining in-flight readers."""
        if self._closed:
            return
        with self._write_lock:
            if self._closed:
                return
            with self._read_gate_cond:
                self._closing = True
                while self._active_readers > 0:
                    self._read_gate_cond.wait()
            self._drain_and_close_readers()
            try:
                self._writer_conn.close()
            except sqlite3.ProgrammingError:
                pass
            self._closed = True

    @property
    def is_closed(self) -> bool:
        """Whether this store's connections have been released."""
        return self._closed

    @classmethod
    def from_json(
        cls, json_path: str | Path, db_path: str | Path
    ) -> "SqliteKnowledgeStore":
        """Migrate from a JSON knowledge store to SQLite."""
        store = KnowledgeStore.load(json_path)
        sqlite_store = cls(db_path)
        sqlite_store.bulk_insert(
            entities=list(store.entities.values()),
            relationships=list(store.relationships),
        )
        return sqlite_store
