"""SQLite-backed chunk repository with FTS5 BM25 search.

Replaces the in-memory flat-file design with a single SQLite database:
- Real BM25 via FTS5's built-in ``bm25()`` ranking (fixes G3, G4).
- Lazy row-level access — no O(n) startup hydration (fixes G2, G5).
- WAL mode for concurrent reads + single writer (fixes G6).
- Dense integer IDs via ``faiss_idx`` column (fixes G8).

Durable embedding storage (hardening Step 08, ADR 3): each chunk row persists
its embedding as a little-endian float32 BLOB alongside an explicit
``embedding_dim``. Insert and load validate byte length, the configured
dimension, and finite values, so vector recovery never depends on transient
``CodeChunk.embedding`` fields. The schema records its version in
``schema_meta``; a legacy v1 database without the embedding column fails closed
with a rebuild instruction rather than being silently reused.

Connection ownership (hardening Step 09, ADR 2): one *serialized* writer
connection (``_writer_conn``) guarded by ``_write_lock`` and many *thread-local*
reader connections. A reader always uses a connection separate from the writer,
opened in autocommit with ``query_only=ON``, so it observes a committed WAL
snapshot and can never see an in-flight writer transaction. ``close()`` and
``load()`` drain in-flight readers through a read gate before tearing down
connections, so an active reader never observes a closed handle.

The term index is a contentless FTS5 table (``content=''`` with
``contentless_delete=1``, storage plan D2): ``tokens_text`` is not stored, every
mutating path writes or deletes its FTS row explicitly in the same transaction,
and index repair is :meth:`rebuild_fts`, a re-tokenization pass over ``content``
that replaces the ``'rebuild'`` command a contentless table cannot run.

See: docs/research/knowcode-architecture-synthesis.md §3.1
     docs/engineering/adr/ (ADR 1, 2, 3, 7)
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from array import array
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from knowcode.data_models import CodeChunk
from knowcode.errors import RepositoryClosedError
from knowcode.storage.chunk_repository import ChunkFileReplacement, ChunkRepository
from knowcode.storage.rewrite_witness import digest_rows, rows_preserved
from knowcode.storage.sqlite_like import LIKE_ESCAPE_CLAUSE, like_contains
from knowcode.utils.entity_identity import (
    absolutize_id,
    normalize_file_identity,
    relativize_id,
    pack_content_hash,
    unpack_content_hash,
)
from knowcode.utils.logger import get_logger
from knowcode.utils.tokenizer import tokenize_code

logger = get_logger(__name__)

# Columns selected for every CodeChunk hydration, in the order ``_row_to_chunk``
# unpacks them. Keep the INSERT column list in the same relative order.
_SELECT_COLUMNS = (
    "chunk_id, entity_id, content, metadata_json, embedding, "
    "embedding_dim, content_hash"
)
_REPO_ROOT_DDL = "CREATE TABLE IF NOT EXISTS repo_root (root TEXT NOT NULL)"

_INSERT_SQL = (
    "INSERT OR REPLACE INTO chunks "
    "(chunk_id, entity_id, content, metadata_json, file_path, "
    "embedding, embedding_dim, content_hash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# The FTS row is written explicitly alongside each chunk write. Resolving the
# rowid through the unique chunk_id — rather than trusting cursor.lastrowid —
# keeps the index correct across INSERT OR REPLACE, which hands a rewritten
# chunk a fresh rowid.
_FTS_INSERT_SQL = (
    "INSERT INTO chunks_fts(rowid, tokens_text) "
    "SELECT rowid, ? FROM chunks WHERE chunk_id = ?"
)
_FTS_DELETE_BY_ID_SQL = (
    "DELETE FROM chunks_fts WHERE rowid IN "
    "(SELECT rowid FROM chunks WHERE chunk_id = ?)"
)
_FTS_DELETE_BY_FILE_SQL = (
    "DELETE FROM chunks_fts WHERE rowid IN "
    "(SELECT rowid FROM chunks WHERE file_path = ?)"
)


class SqliteChunkRepository(ChunkRepository):
    """SQLite + FTS5 implementation of ChunkRepository.

    Uses WAL mode for safe concurrent reads with a single serialized writer.
    The FTS5 virtual table provides real BM25 ranking for sparse search.

    Thread safety follows ADR 2: the writer connection is shared but every
    write/schema operation is serialized by ``_write_lock``; each thread gets
    its own reader connection that sees only committed snapshots. This mirrors
    the real service topology — one shared store instance, many request threads
    — without exposing a writer's open transaction to readers.
    """

    SCHEMA_VERSION = 4
    # v1 predates durable embeddings; v3 still stored tokens_text to back an
    # external-content FTS table. Neither migrates without a rebuild, so both
    # fail closed (ADR 3 compatibility).
    LEGACY_SCHEMA_VERSION = 1
    # The contentless_delete FTS5 option arrived in SQLite 3.43. Below it the
    # CREATE VIRTUAL TABLE fails with "unrecognized option", which is loud but
    # names nothing a caller can act on; this floor states the requirement
    # (BL-13).
    MINIMUM_SQLITE_VERSION = (3, 43, 0)

    def __init__(
        self,
        db_path: str | Path,
        *,
        dimension: int | None = None,
    ) -> None:
        """Open or create the SQLite chunk database.

        Args:
            db_path: Path to the ``.db`` file.  Created if it does not exist.
            dimension: Optional configured embedding dimension. When set,
                inserts validate that every embedding matches it, enforcing
                one dense dimension per generation (ADR 3).
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._configured_dimension = int(dimension) if dimension is not None else None

        if sqlite3.sqlite_version_info < self.MINIMUM_SQLITE_VERSION:
            floor = ".".join(str(part) for part in self.MINIMUM_SQLITE_VERSION)
            raise RuntimeError(
                f"SqliteChunkRepository needs SQLite {floor} or newer for the "
                "contentless_delete FTS5 option; this interpreter links "
                f"SQLite {sqlite3.sqlite_version}."
            )

        # An in-memory database (``:memory:``) is per-connection, so the
        # writer/reader split would hand readers an isolated empty database.
        # Route in-memory databases through a unique shared-cache URI so every
        # connection of this one instance shares a single in-memory database
        # (file-backed databases are unaffected and keep their WAL isolation).
        self._in_memory = str(self._db_path) == ":memory:"
        self._in_memory_uri = f"file:knowcode_chunk_{id(self)}?mode=memory&cache=shared"

        # Serialized writer connection (ADR 2). ``isolation_level="DEFERRED"``
        # is deferred mode so ``with self._writer_conn:`` issues a real
        # BEGIN/commit/rollback around multi-statement transactions. The writer
        # is shared across threads but every access is guarded by _write_lock.
        self._write_lock = threading.Lock()
        self._writer_conn = self._open_writer_conn()

        # Read gate: a counter + condition so close()/load() can wait for
        # in-flight readers to release their lease before tearing connections
        # down. Lock order is _write_lock -> _read_gate, never the reverse.
        self._read_gate_cond = threading.Condition()
        self._active_readers = 0
        self._closing = False
        self._closed = False

        # Thread-local reader connections (one per execution context). Tracked
        # in a set guarded by its own lock so close()/load() can drain them
        # deterministically. An epoch invalidates cached thread-local handles
        # after a path swap/load.
        self._thread_local = threading.local()
        self._reader_conns: set[sqlite3.Connection] = set()
        self._reader_conns_lock = threading.Lock()
        self._reader_epoch = 0

        self._generation_counter = 0

        # Ids are stored relative to this root and hydrated back to absolute
        # on the way out, so a generation's size does not depend on where
        # it was built (ADR 1, ADR 10). Empty until set_repo_root binds it.
        self._repo_root = ""

        self._init_schema()

    # ------------------------------------------------------------------
    # Connection ownership (ADR 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_writer_pragmas(conn: sqlite3.Connection) -> None:
        """Set durability/locking PRAGMAs on a writer connection.

        ``busy_timeout`` is already the 5000ms default set by
        ``sqlite3.connect(timeout=5.0)``; setting it explicitly makes the
        contract legible rather than fixing a gap.
        """
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    def _open_writer_conn(self) -> sqlite3.Connection:
        """Open a fresh serialized writer connection (deferred transactions)."""
        if self._in_memory:
            conn = sqlite3.connect(
                self._in_memory_uri,
                uri=True,
                check_same_thread=False,
                isolation_level="DEFERRED",
            )
        else:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level="DEFERRED",
            )
        return conn

    def _open_reader_conn(self) -> sqlite3.Connection:
        """Open a new autocommit reader connection for the current path.

        Readers are opened in autocommit (``isolation_level=None``) with
        ``query_only=ON`` so a reader can never accidentally write. WAL is a
        per-database property already enabled on the writer, so new readers
        inherit snapshot isolation automatically.
        """
        if self._in_memory:
            conn = sqlite3.connect(
                self._in_memory_uri,
                uri=True,
                check_same_thread=True,
                isolation_level=None,
                timeout=5.0,
            )
        else:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=True,
                isolation_level=None,
                timeout=5.0,
            )
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _reader_conn(self) -> sqlite3.Connection:
        """Return the calling thread's reader connection, creating it lazily.

        A thread-local handle is cached together with the reader epoch. After a
        ``load()`` swaps the path, the epoch bump invalidates stale cached
        handles so the next read reopens against the new path.
        """
        tls = self._thread_local
        cached: sqlite3.Connection | None = getattr(tls, "conn", None)
        if cached is not None and getattr(tls, "epoch", -1) == self._reader_epoch:
            return cached
        if cached is not None:
            # Stale handle from a pre-load epoch; best-effort close.
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

        Entering tests ``_closing``/``_closed`` and increments
        ``_active_readers`` atomically under the gate condition so a reader
        cannot slip past a teardown that is already in progress. ``close()`` and
        ``load()`` set ``_closing`` under ``_write_lock`` then wait here until
        ``_active_readers`` drains to zero, so they cannot close a connection an
        in-flight reader still holds.

        Caveat: cached thread-local connections are reused across requests by a
        thread pool. If a pooled thread is retired without closing the store,
        its reader connection leaks until process exit. The per-generation
        reader-lease handoff that retires old resources is Step 18.
        """
        with self._read_gate_cond:
            if self._closing or self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
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
        """Close every tracked reader connection and invalidate caches.

        The caller holds ``_write_lock`` and has already set ``_closing`` and
        waited for ``_active_readers == 0``, so no reader is in flight.
        """
        with self._reader_conns_lock:
            conns = list(self._reader_conns)
            self._reader_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # Invalidate any remaining thread-local caches so the next read on a
        # surviving thread reopens against the new path.
        self._reader_epoch += 1

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The on-disk schema version recorded in ``schema_meta``."""
        return self.SCHEMA_VERSION

    def _init_schema(self) -> None:
        """Create or validate the schema, failing closed on legacy v1."""
        conn = self._writer_conn
        # WAL is a per-database property and must execute outside any open
        # transaction; deferred mode defers BEGIN until the first DML statement,
        # so a plain PRAGMA here never starts one.
        conn.execute("PRAGMA journal_mode=WAL")
        self._apply_writer_pragmas(conn)

        with conn:
            existing = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'chunks'"
            ).fetchone()
            if existing is None:
                self._create_schema()
                return

            # An existing chunks table must already be the durable-embedding
            # schema; adding ``CREATE TABLE IF NOT EXISTS`` would not upgrade
            # it, so validate explicitly and fail closed on legacy v1.
            self._validate_existing_schema()
            self._ensure_schema_meta()
        self._load_repo_root()

    def _create_schema(self) -> None:
        """Create the full v4 schema for a fresh database."""
        self._writer_conn.execute("""
            CREATE TABLE chunks (
                rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id   TEXT    NOT NULL UNIQUE,
                entity_id  TEXT    NOT NULL,
                content    TEXT    NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                file_path  TEXT    NOT NULL DEFAULT '',
                embedding  BLOB,
                embedding_dim INTEGER,
                content_hash BLOB
            )
        """)
        self._writer_conn.execute(
            "CREATE INDEX idx_chunks_entity_id ON chunks (entity_id)"
        )
        self._writer_conn.execute(
            "CREATE INDEX idx_chunks_file_path ON chunks (file_path)"
        )
        self._writer_conn.execute(
            "CREATE INDEX idx_chunks_content_hash ON chunks (content_hash)"
        )

        # D2: a contentless term index. The table holds the inverted index
        # and nothing else — no content table to mirror, no tokens_text
        # column. contentless_delete=1 is what makes a plain DELETE legal;
        # without it an FTS row can never be removed and incremental
        # re-indexing silently corrupts search (BL-13). Every FTS row is
        # written and deleted by the repository, in the same transaction as
        # its chunk row, so no sync triggers exist. unicode61 only splits on
        # whitespace: the Python tokenizer has already handled
        # camelCase/snake_case splitting.
        self._writer_conn.execute("""
            CREATE VIRTUAL TABLE chunks_fts
            USING fts5(
                tokens_text,
                content='',
                contentless_delete=1,
                tokenize='unicode61'
            )
        """)

        self._writer_conn.execute(_REPO_ROOT_DDL)
        self._writer_conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        self._writer_conn.execute(
            "INSERT INTO schema_meta (version) VALUES (?)",
            (self.SCHEMA_VERSION,),
        )

    def _validate_existing_schema(self) -> None:
        """Fail closed when an existing chunks table predates durable embeddings."""
        columns = {
            row[1]
            for row in self._writer_conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "embedding" not in columns or "embedding_dim" not in columns:
            raise ValueError(
                f"Legacy SQLite chunk schema (v{self.LEGACY_SCHEMA_VERSION}) at "
                f"{self._db_path} has no durable embedding column. Rebuild the "
                "semantic index with `knowcode build`; embeddings cannot be "
                "migrated without an embedding provider."
            )
        if "tokens_text" in columns:
            raise ValueError(
                f"SQLite chunk schema at {self._db_path} still stores "
                "tokens_text (v3, before the contentless FTS index). Rebuild "
                "the index with `knowcode build`; the term index cannot be "
                "recreated from a column this build no longer writes."
            )
        if "content_hash" not in columns:
            raise ValueError(
                f"SQLite chunk schema at {self._db_path} predates the "
                "first-class content_hash column. Rebuild the semantic index "
                "with `knowcode build`; the digest lives only in the retired "
                "metadata_json key and this build no longer reads it."
            )

    def _ensure_schema_meta(self) -> None:
        """Record the current schema version for an already-valid database."""
        self._writer_conn.execute(_REPO_ROOT_DDL)
        self._writer_conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
        )
        row = self._writer_conn.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()
        if row is None:
            self._writer_conn.execute(
                "INSERT INTO schema_meta (version) VALUES (?)",
                (self.SCHEMA_VERSION,),
            )

    # ------------------------------------------------------------------
    # Repository root (ADR 10)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Embedding codec (ADR 3)
    # ------------------------------------------------------------------

    def _encode_embedding(
        self,
        values: list[float] | None,
    ) -> tuple[bytes | None, int | None]:
        """Serialize an embedding as a little-endian float32 BLOB.

        Returns ``(blob, dimension)``. A null embedding produces ``(None, None)``.
        Validates finite values and, when a dimension is configured, that the
        embedding matches it.
        """
        if values is None:
            return None, None
        if self._configured_dimension is not None and len(values) != (
            self._configured_dimension
        ):
            raise ValueError(
                f"Embedding dimension {len(values)} does not match configured "
                f"dimension {self._configured_dimension}."
            )
        for value in values:
            if not math.isfinite(value):
                raise ValueError(
                    "Embedding contains a non-finite value (NaN/inf); refusing "
                    "to persist an invalid dense vector."
                )
        packed = array("f", values)
        if sys.byteorder == "big":
            packed.byteswap()
        return packed.tobytes(), len(values)

    def _decode_embedding(
        self,
        blob: bytes | None,
        dimension: int | None,
    ) -> list[float] | None:
        """Deserialize a float32 BLOB, validating byte length and finiteness."""
        if blob is None:
            return None
        expected = None if dimension is None else dimension * 4
        if expected is not None and len(blob) != expected:
            raise ValueError(
                f"Embedding BLOB is {len(blob)} bytes but dimension "
                f"{dimension} implies {expected} bytes."
            )
        if len(blob) % 4 != 0:
            raise ValueError(
                f"Embedding BLOB length {len(blob)} is not a multiple of 4."
            )
        packed = array("f")
        packed.frombytes(blob)
        if sys.byteorder == "big":
            packed.byteswap()
        decoded = list(packed)
        for value in decoded:
            if not math.isfinite(value):
                raise ValueError("Persisted embedding contains a non-finite value.")
        return decoded

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _file_path_from_entity_id(entity_id: str) -> str:
        """Extract the file-path prefix from an entity ID.

        Entity IDs follow the convention ``file_path::qualified_name``.
        If there is no ``::`` separator the entire string is returned.
        """
        idx = entity_id.find("::")
        return entity_id[:idx] if idx >= 0 else entity_id

    @staticmethod
    def _fts_tokens(chunk: CodeChunk) -> str:
        """Render a chunk's tokens in the whitespace-joined form FTS5 indexes."""
        return " ".join(chunk.tokens) if chunk.tokens else ""

    def _purge_fts_rows(self, stored_ids: list[str]) -> None:
        """Delete the FTS rows of chunk ids about to be rewritten.

        ``INSERT OR REPLACE`` hands a rewritten chunk a fresh rowid, so its
        previous FTS row is left pointing at nothing and would keep matching
        the old terms forever — the silent half of BL-13. Runs on the writer
        inside the caller's transaction, before the replacement rows land.
        """
        self._writer_conn.executemany(
            _FTS_DELETE_BY_ID_SQL, [(stored_id,) for stored_id in stored_ids]
        )

    def _chunk_to_row(
        self,
        chunk: CodeChunk,
        file_path: str | None = None,
    ) -> tuple[Any, ...]:
        """Build a full insert row for a chunk.

        ``file_path`` defaults to the entity-id prefix; ``replace_file`` passes
        the canonical identity so a file's rows share one lookup key.
        """
        stored_metadata = {
            key: value for key, value in chunk.metadata.items() if key != "content_hash"
        }
        metadata_json = json.dumps(stored_metadata) if stored_metadata else "{}"
        stored_path = (
            file_path
            if file_path is not None
            else (self._file_path_from_entity_id(chunk.entity_id))
        )
        embedding_blob, embedding_dim = self._encode_embedding(chunk.embedding)
        return (
            self._store_id(chunk.id),
            self._store_id(chunk.entity_id),
            chunk.content,
            metadata_json,
            self._store_id(stored_path),
            embedding_blob,
            embedding_dim,
            pack_content_hash(chunk.metadata.get("content_hash")),
        )

    def _row_to_chunk(self, row: tuple[Any, ...]) -> CodeChunk:
        """Convert a database row to a CodeChunk.

        Expected column order:
            chunk_id, entity_id, content, metadata_json, embedding,
            embedding_dim, content_hash

        ``tokens`` is an indexing-time derivation of ``content``; its durable
        form is the FTS row itself, so a hydrated chunk carries none.
        """
        (
            chunk_id,
            entity_id,
            content,
            metadata_json,
            embedding_blob,
            embedding_dim,
            content_hash,
        ) = row
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if content_hash is not None:
            metadata["content_hash"] = unpack_content_hash(content_hash)
        embedding = self._decode_embedding(embedding_blob, embedding_dim)
        return CodeChunk(
            id=self._load_id(chunk_id),
            entity_id=self._load_id(entity_id),
            content=content,
            tokens=[],
            embedding=embedding,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # ChunkRepository interface
    # ------------------------------------------------------------------

    def add(self, chunk: CodeChunk) -> None:
        """Insert or replace a single chunk."""
        row = self._chunk_to_row(chunk)
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                self._purge_fts_rows([row[0]])
                self._writer_conn.execute(_INSERT_SQL, row)
                self._writer_conn.execute(
                    _FTS_INSERT_SQL, (self._fts_tokens(chunk), row[0])
                )

    def add_batch(self, chunks: list[CodeChunk]) -> None:
        """Insert multiple chunks in a single transaction."""
        if not chunks:
            return
        rows = [self._chunk_to_row(chunk) for chunk in chunks]
        fts_rows = [
            (self._fts_tokens(chunk), row[0]) for chunk, row in zip(chunks, rows)
        ]
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                self._purge_fts_rows([row[0] for row in rows])
                self._writer_conn.executemany(_INSERT_SQL, rows)
                self._writer_conn.executemany(_FTS_INSERT_SQL, fts_rows)

    def get(self, chunk_id: str) -> Optional[CodeChunk]:
        """Fetch a single chunk by ID (lazy, single-row SELECT)."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE chunk_id = ?",
                (self._store_id(chunk_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def get_by_entity(self, entity_id: str) -> list[CodeChunk]:
        """Fetch all chunks belonging to an entity."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE entity_id = ?",
                (self._store_id(entity_id),),
            )
            rows = cursor.fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def search_by_tokens(self, tokens: list[str], limit: int = 10) -> list[CodeChunk]:
        """Real BM25 search via FTS5.

        Builds an OR-match expression from the provided tokens and ranks
        results using FTS5's built-in ``bm25()`` function.  IDF weighting
        and length normalization come for free.
        """
        if not tokens:
            return []

        # Sanitize tokens: strip quotes, keep only alphanumeric.
        safe_tokens = []
        for t in tokens:
            cleaned = "".join(c for c in t if c.isalnum())
            if cleaned:
                safe_tokens.append(cleaned)

        if not safe_tokens:
            return []

        # FTS5 MATCH: OR of all tokens.
        match_expr = " OR ".join(safe_tokens)

        try:
            with self._read_lease() as conn:
                cursor = conn.execute(
                    f"""SELECT c.{_SELECT_COLUMNS.replace(", ", ", c.")}
                       FROM chunks_fts fts
                       JOIN chunks c ON c.rowid = fts.rowid
                       WHERE chunks_fts MATCH ?
                       ORDER BY bm25(chunks_fts)
                       LIMIT ?""",
                    (match_expr, limit),
                )
                rows = cursor.fetchall()
            return [self._row_to_chunk(row) for row in rows]
        except sqlite3.OperationalError as e:
            # Malformed MATCH expression — return empty rather than crash, but log it.
            logger.warning("FTS5 MATCH failed for expression '%s': %s", match_expr, e)
            return []

    def search_exact(self, pattern: str, limit: int = 10) -> list[CodeChunk]:
        """Search chunks whose content contains ``pattern`` as a literal substring.

        Two semantics, both deliberate. The match is literal, so ``_`` and
        ``%`` are ordinary characters rather than wildcards (BL-15). The match
        is case-insensitive over ASCII, which is ``LIKE``'s own behaviour.
        """
        if not pattern:
            return []

        with self._read_lease() as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM chunks "
                f"WHERE content LIKE ? {LIKE_ESCAPE_CLAUSE} LIMIT ?",
                (like_contains(pattern), limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def remove_by_file(self, file_path: str) -> list[str]:
        """Remove all chunks associated with a canonical file identity."""
        file_identity = normalize_file_identity(file_path)
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                stored_path = self._store_id(file_identity)
                cursor = self._writer_conn.execute(
                    "SELECT chunk_id FROM chunks WHERE file_path = ?",
                    (stored_path,),
                )
                removed_ids = [self._load_id(row[0]) for row in cursor]

                # The FTS rows go first: they resolve their rowids through
                # the chunk rows they belong to. A plain DELETE is legal only
                # under contentless_delete=1 (BL-13).
                self._writer_conn.execute(
                    _FTS_DELETE_BY_FILE_SQL,
                    (stored_path,),
                )
                self._writer_conn.execute(
                    "DELETE FROM chunks WHERE file_path = ?",
                    (stored_path,),
                )
                return removed_ids

    def replace_file(
        self,
        file_path: str | Path,
        chunks: list[CodeChunk],
    ) -> ChunkFileReplacement:
        """Atomically replace every chunk for one canonical file identity.

        Runs as one writer transaction: the previous chunks for the normalized
        ``file_path`` are deleted and the new ``chunks`` inserted together, so
        a failure between the two leaves the prior generation searchable. Each
        chunk write carries its FTS row explicitly, in the same transaction.
        """
        file_identity = normalize_file_identity(file_path)
        rows = [self._chunk_to_row(chunk, file_identity) for chunk in chunks]
        fts_rows = [
            (self._fts_tokens(chunk), row[0]) for chunk, row in zip(chunks, rows)
        ]

        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                stored_path = self._store_id(file_identity)
                previous = [
                    self._load_id(row[0])
                    for row in self._writer_conn.execute(
                        "SELECT chunk_id FROM chunks WHERE file_path = ?",
                        (stored_path,),
                    )
                ]
                self._writer_conn.execute(
                    _FTS_DELETE_BY_FILE_SQL,
                    (stored_path,),
                )
                self._writer_conn.execute(
                    "DELETE FROM chunks WHERE file_path = ?",
                    (stored_path,),
                )
                if rows:
                    self._commit_rows(rows)
                    self._writer_conn.executemany(_FTS_INSERT_SQL, fts_rows)
                committed = tuple(self._load_id(row[0]) for row in rows)

            self._generation_counter += 1
            generation_metadata: dict[str, Any] = {
                "file_path": file_identity,
                "previous_count": len(previous),
                "committed_count": len(committed),
                "generation": self._generation_counter,
                "schema_version": self.SCHEMA_VERSION,
            }
            return ChunkFileReplacement(
                file_path=file_identity,
                previous_chunk_ids=tuple(previous),
                committed_chunk_ids=committed,
                generation_metadata=generation_metadata,
            )

    def _commit_rows(self, rows: list[tuple[Any, ...]]) -> None:
        """Insert replacement rows inside the active transaction.

        This is an intentional seam: callers and tests drive the surrounding
        transaction through :meth:`replace_file`, while fault injection can
        substitute this step to prove the DELETE/INSERT pair rolls back
        together. It runs against the writer connection and never opens its own
        transaction.
        """
        self._writer_conn.executemany(_INSERT_SQL, rows)

    def get_chunk_id_by_hash(self, content_hash: str) -> Optional[str]:
        """Find a chunk_id given a content_hash (from metadata)."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT chunk_id FROM chunks WHERE content_hash = ? LIMIT 1",
                (pack_content_hash(content_hash),),
            )
            row = cursor.fetchone()
        return self._load_id(row[0]) if row else None

    def get_all_file_paths(self) -> set[str]:
        """Return a set of all file paths currently in the repository."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT file_path FROM chunks WHERE file_path != ''"
            )
            rows = cursor.fetchall()
        return {self._load_id(row[0]) for row in rows}

    def clear(self) -> None:
        """Remove all chunks and reset the FTS index."""
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                self._writer_conn.execute("DELETE FROM chunks")
                # A plain DELETE, not the 'rebuild' command the external-content
                # design used: a contentless table cannot run 'rebuild', and
                # after clearing the chunks there is nothing left to rebuild
                # from anyway.
                self._writer_conn.execute("DELETE FROM chunks_fts")

    def rebuild_fts(self) -> None:
        """Rebuild the term index by re-tokenizing stored content.

        The replacement for the one-statement ``'rebuild'`` command a
        contentless FTS5 table cannot run: clears the index and re-derives
        every row from ``chunks.content`` through ``tokenize_code`` — the same
        derivation the chunker used, so the rebuilt index matches the one the
        write path produced. One tokenization pass over the corpus, no source
        tree access, no network. Holds until Phase F removes ``content``;
        after that, repair is a re-index.
        """
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            with self._writer_conn:
                self._writer_conn.execute("DELETE FROM chunks_fts")
                rows = self._writer_conn.execute(
                    "SELECT rowid, content FROM chunks"
                ).fetchall()
                self._writer_conn.executemany(
                    "INSERT INTO chunks_fts(rowid, tokens_text) VALUES (?, ?)",
                    (
                        (rowid, " ".join(tokenize_code(content)))
                        for rowid, content in rows
                    ),
                )

    def count(self) -> int:
        """Return the number of stored chunks."""
        with self._read_lease() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM chunks")
            return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # FAISS index mapping
    # ------------------------------------------------------------------

    def get_faiss_idx(self, chunk_id: str) -> Optional[int]:
        """Return the rowid (used as FAISS index) for a chunk_id."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT rowid FROM chunks WHERE chunk_id = ?",
                (self._store_id(chunk_id),),
            )
            row = cursor.fetchone()
        return row[0] if row is not None else None

    def get_chunk_id_by_faiss_idx(self, faiss_idx: int) -> Optional[str]:
        """Resolve a FAISS integer index back to a chunk_id."""
        with self._read_lease() as conn:
            cursor = conn.execute(
                "SELECT chunk_id FROM chunks WHERE rowid = ?",
                (faiss_idx,),
            )
            row = cursor.fetchone()
        return self._load_id(row[0]) if row is not None else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compact(self) -> None:
        """Rewrite the database into the fewest pages its rows need.

        Belongs to the moment a staged artifact is final, immediately before
        the generation that owns it is digested. Running it afterwards would
        change bytes the manifest has already checksummed, and folding it into
        :meth:`close` would make every reader that opens and closes a
        generation rewrite the whole file.

        The rewrite lands in the write-ahead log and truncates the main file at
        once, so whoever closes afterwards publishes the compact copy. Nothing
        a row holds changes, durable embedding BLOBs included, and the bracket
        is what proves it rather than asserting it (BL-8).
        """
        with self._write_lock:
            if self._closed:
                raise RepositoryClosedError(
                    "SqliteChunkRepository is closed; open a new instance."
                )
            # The bracket opens before anything else this method does, so no
            # step of it can lose a row where the witness cannot see it.
            with rows_preserved(self._rewrite_witness, "chunks.db"):
                # VACUUM refuses to run inside a transaction. Every write path
                # here commits its own, so this only settles a stray one.
                self._writer_conn.commit()
                self._rewrite()

    def _rewrite(self) -> None:
        """Rewrite the staged file. Every staged rewrite belongs here.

        Separate from :meth:`compact` so that whatever this grows into stays
        inside the losslessness bracket rather than beside it.
        """
        self._writer_conn.execute("VACUUM")

    def _rewrite_witness(self) -> dict[str, str]:
        """Digest the row sets a staged rewrite must leave alone.

        ``embedded_chunk_ids`` covers *which* chunks carry a durable embedding,
        not how many. The count is what a manifest already records, and it is
        derived from this same file, which is the blind spot D1's durable
        embedding guard shares (BL-8).
        """
        chunk_ids = [
            self._load_id(row[0])
            for row in self._writer_conn.execute("SELECT chunk_id FROM chunks")
        ]
        embedded = [
            self._load_id(row[0])
            for row in self._writer_conn.execute(
                "SELECT chunk_id FROM chunks WHERE embedding IS NOT NULL"
            )
        ]
        return {
            "chunk_ids": digest_rows(chunk_ids),
            "embedded_chunk_ids": digest_rows(embedded),
        }

    def close(self) -> None:
        """Close all connections idempotently after draining in-flight readers.

        The existing-connection swallow is narrowed to
        ``sqlite3.ProgrammingError`` (already closed) at debug level; any other
        close-time error propagates.
        """
        if self._closed:
            return
        with self._write_lock:
            if self._closed:
                return
            # Stop new readers from entering the gate, then drain in-flight ones.
            with self._read_gate_cond:
                self._closing = True
                while self._active_readers > 0:
                    self._read_gate_cond.wait()
            self._drain_and_close_readers()
            try:
                self._writer_conn.close()
            except sqlite3.ProgrammingError as e:
                logger.debug("Writer connection already closed: %s", e)
            self._closed = True

    @property
    def is_closed(self) -> bool:
        """Whether this repository's connections have been released."""
        return self._closed

    def save(self, path: Path) -> None:
        """Persist data (no-op as SQLite auto-persists)."""
        pass

    def load(self, path: Path) -> None:
        """Load database from the given directory path if different.

        Drains in-flight readers, closes all connections, swaps the path, and
        re-initializes/validates the schema at the new location. Also handles
        automatic migration from legacy ``chunks.json`` if present. A fresh
        target initializes and validates the current schema.
        """
        db_path = path / "chunks.db"
        if db_path.resolve() == self._db_path.resolve():
            return
        if self._closed:
            raise RepositoryClosedError(
                "SqliteChunkRepository is closed; open a new instance."
            )
        with self._write_lock:
            # Drain readers before swapping connections.
            with self._read_gate_cond:
                self._closing = True
                while self._active_readers > 0:
                    self._read_gate_cond.wait()
            self._drain_and_close_readers()
            try:
                self._writer_conn.close()
            except sqlite3.ProgrammingError as e:
                logger.debug("Writer connection already closed during re-load: %s", e)
            self._db_path = db_path
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._in_memory = str(self._db_path) == ":memory:"
            self._writer_conn = self._open_writer_conn()
            self._closing = False  # reopen the read gate for the new path
            self._init_schema()

    def get_all(self) -> list[CodeChunk]:
        """Fetch all chunks from the database (embeddings restored)."""
        with self._read_lease() as conn:
            cursor = conn.execute(f"SELECT {_SELECT_COLUMNS} FROM chunks")
            rows = cursor.fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def iter_embeddings(
        self, *, batch_size: int = 512
    ) -> Iterator[tuple[str, list[float]]]:
        """Stream every durable embedding as ``(chunk_id, vector)`` pairs.

        This is the source a derived vector plane is rebuilt from, so it reads
        only the two columns a rebuild needs and paginates by ``rowid`` under a
        fresh short read lease per batch. Holding one lease across the whole
        stream would block :meth:`close` for as long as a consumer kept the
        iterator alive.

        Rows with a NULL ``embedding`` carry no durable vector and are skipped;
        callers that need a count of what was placed should count what they
        consume.

        Args:
            batch_size: Rows fetched per lease. Bounds peak memory.

        Yields:
            ``(chunk_id, embedding)`` in ascending ``rowid`` order.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        after = -1
        while True:
            with self._read_lease() as conn:
                rows = conn.execute(
                    "SELECT rowid, chunk_id, embedding, embedding_dim FROM chunks "
                    "WHERE embedding IS NOT NULL AND rowid > ? "
                    "ORDER BY rowid LIMIT ?",
                    (after, batch_size),
                ).fetchall()
            if not rows:
                return
            for rowid, stored_chunk_id, blob, dimension in rows:
                chunk_id = self._load_id(stored_chunk_id)
                after = rowid
                embedding = self._decode_embedding(blob, dimension)
                if embedding is not None:
                    yield chunk_id, embedding
