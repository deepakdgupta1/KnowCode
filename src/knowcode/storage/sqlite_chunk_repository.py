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

See: docs/research/knowcode-architecture-synthesis.md §3.1
     docs/architecture/hardening-contracts.md (ADR 1, 3, 7)
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from array import array
from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import CodeChunk
from knowcode.storage.chunk_repository import ChunkFileReplacement, ChunkRepository
from knowcode.utils.entity_identity import normalize_file_identity
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

# Columns selected for every CodeChunk hydration, in the order ``_row_to_chunk``
# unpacks them. Keep the INSERT column list in the same relative order.
_SELECT_COLUMNS = (
    "chunk_id, entity_id, content, tokens_text, metadata_json, "
    "embedding, embedding_dim"
)
_INSERT_SQL = (
    "INSERT OR REPLACE INTO chunks "
    "(chunk_id, entity_id, content, tokens_text, metadata_json, file_path, "
    "embedding, embedding_dim) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


class SqliteChunkRepository(ChunkRepository):
    """SQLite + FTS5 implementation of ChunkRepository.

    Uses WAL mode for safe concurrent reads with a single writer.
    The FTS5 virtual table provides real BM25 ranking for sparse search.
    """

    SCHEMA_VERSION = 2
    # Baseline chunk schema had no durable embedding column; it cannot be
    # losslessly migrated without an embedding provider, so legacy v1 stores
    # fail closed (ADR 3 compatibility).
    LEGACY_SCHEMA_VERSION = 1

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
        self._configured_dimension = (
            int(dimension) if dimension is not None else None
        )

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._write_lock = threading.Lock()
        self._generation_counter = 0

        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The on-disk schema version recorded in ``schema_meta``."""
        return self.SCHEMA_VERSION

    def _init_schema(self) -> None:
        """Create or validate the schema, failing closed on legacy v1."""
        with self._conn:
            # Enable WAL mode for concurrent readers + single writer.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

            existing = self._conn.execute(
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

    def _create_schema(self) -> None:
        """Create the full v2 schema for a fresh database."""
        self._conn.execute("""
            CREATE TABLE chunks (
                rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id   TEXT    NOT NULL UNIQUE,
                entity_id  TEXT    NOT NULL,
                content    TEXT    NOT NULL DEFAULT '',
                tokens_text TEXT   NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                file_path  TEXT    NOT NULL DEFAULT '',
                embedding  BLOB,
                embedding_dim INTEGER
            )
        """)
        self._conn.execute(
            "CREATE INDEX idx_chunks_entity_id ON chunks (entity_id)"
        )
        self._conn.execute(
            "CREATE INDEX idx_chunks_file_path ON chunks (file_path)"
        )

        # FTS5 external-content table backed by chunks.tokens_text.
        # Using unicode61 tokenizer — our Python tokenizer already
        # handles camelCase/snake_case splitting, so FTS5 only needs
        # to split on whitespace.
        self._conn.execute("""
            CREATE VIRTUAL TABLE chunks_fts
            USING fts5(
                tokens_text,
                content='chunks',
                content_rowid='rowid',
                tokenize='unicode61'
            )
        """)

        # Triggers to keep the FTS index in sync with the content table.
        self._conn.executescript("""
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, tokens_text)
                VALUES (new.rowid, new.tokens_text);
            END;

            CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, tokens_text)
                VALUES ('delete', old.rowid, old.tokens_text);
            END;

            CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, tokens_text)
                VALUES ('delete', old.rowid, old.tokens_text);
                INSERT INTO chunks_fts(rowid, tokens_text)
                VALUES (new.rowid, new.tokens_text);
            END;
        """)

        self._conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL)"
        )
        self._conn.execute(
            "INSERT INTO schema_meta (version) VALUES (?)",
            (self.SCHEMA_VERSION,),
        )

    def _validate_existing_schema(self) -> None:
        """Fail closed when an existing chunks table predates durable embeddings."""
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "embedding" not in columns or "embedding_dim" not in columns:
            raise ValueError(
                f"Legacy SQLite chunk schema (v{self.LEGACY_SCHEMA_VERSION}) at "
                f"{self._db_path} has no durable embedding column. Rebuild the "
                "semantic index with `knowcode build`; embeddings cannot be "
                "migrated without an embedding provider."
            )

    def _ensure_schema_meta(self) -> None:
        """Record the current schema version for an already-valid database."""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
        )
        row = self._conn.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta (version) VALUES (?)",
                (self.SCHEMA_VERSION,),
            )

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

    def _chunk_to_row(
        self,
        chunk: CodeChunk,
        file_path: str | None = None,
    ) -> tuple[Any, ...]:
        """Build a full insert row for a chunk.

        ``file_path`` defaults to the entity-id prefix; ``replace_file`` passes
        the canonical identity so a file's rows share one lookup key.
        """
        tokens_text = " ".join(chunk.tokens) if chunk.tokens else ""
        metadata_json = json.dumps(chunk.metadata) if chunk.metadata else "{}"
        stored_path = file_path if file_path is not None else (
            self._file_path_from_entity_id(chunk.entity_id)
        )
        embedding_blob, embedding_dim = self._encode_embedding(chunk.embedding)
        return (
            chunk.id,
            chunk.entity_id,
            chunk.content,
            tokens_text,
            metadata_json,
            stored_path,
            embedding_blob,
            embedding_dim,
        )

    def _row_to_chunk(self, row: tuple[Any, ...]) -> CodeChunk:
        """Convert a database row to a CodeChunk.

        Expected column order:
            chunk_id, entity_id, content, tokens_text, metadata_json,
            embedding, embedding_dim
        """
        (
            chunk_id,
            entity_id,
            content,
            tokens_text,
            metadata_json,
            embedding_blob,
            embedding_dim,
        ) = row
        tokens = tokens_text.split() if tokens_text else []
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        embedding = self._decode_embedding(embedding_blob, embedding_dim)
        return CodeChunk(
            id=chunk_id,
            entity_id=entity_id,
            content=content,
            tokens=tokens,
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
            with self._conn:
                self._conn.execute(_INSERT_SQL, row)

    def add_batch(self, chunks: list[CodeChunk]) -> None:
        """Insert multiple chunks in a single transaction."""
        if not chunks:
            return
        rows = [self._chunk_to_row(chunk) for chunk in chunks]
        with self._write_lock:
            with self._conn:
                self._conn.executemany(_INSERT_SQL, rows)

    def get(self, chunk_id: str) -> Optional[CodeChunk]:
        """Fetch a single chunk by ID (lazy, single-row SELECT)."""
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def get_by_entity(self, entity_id: str) -> list[CodeChunk]:
        """Fetch all chunks belonging to an entity."""
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE entity_id = ?",
            (entity_id,),
        )
        return [self._row_to_chunk(row) for row in cursor]

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
            cursor = self._conn.execute(
                f"""SELECT c.{_SELECT_COLUMNS.replace(', ', ', c.')}
                   FROM chunks_fts fts
                   JOIN chunks c ON c.rowid = fts.rowid
                   WHERE chunks_fts MATCH ?
                   ORDER BY bm25(chunks_fts)
                   LIMIT ?""",
                (match_expr, limit),
            )
            return [self._row_to_chunk(row) for row in cursor]
        except sqlite3.OperationalError as e:
            # Malformed MATCH expression — return empty rather than crash, but log it.
            logger.warning("FTS5 MATCH failed for expression '%s': %s", match_expr, e)
            return []

    def search_exact(self, pattern: str, limit: int = 10) -> list[CodeChunk]:
        """Search chunks by exact substring match using LIKE."""
        if not pattern:
            return []

        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE content LIKE ? LIMIT ?",
            (f"%{pattern}%", limit),
        )
        return [self._row_to_chunk(row) for row in cursor]

    def remove_by_file(self, file_path: str) -> list[str]:
        """Remove all chunks associated with a canonical file identity."""
        file_identity = normalize_file_identity(file_path)
        with self._write_lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT chunk_id FROM chunks WHERE file_path = ?",
                    (file_identity,),
                )
                removed_ids = [row[0] for row in cursor]

                self._conn.execute(
                    "DELETE FROM chunks WHERE file_path = ?",
                    (file_identity,),
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
        a failure between the two leaves the prior generation searchable. FTS
        triggers keep the sparse index consistent within the same transaction.
        """
        file_identity = normalize_file_identity(file_path)
        rows = [self._chunk_to_row(chunk, file_identity) for chunk in chunks]

        with self._write_lock:
            with self._conn:
                previous = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT chunk_id FROM chunks WHERE file_path = ?",
                        (file_identity,),
                    )
                ]
                self._conn.execute(
                    "DELETE FROM chunks WHERE file_path = ?",
                    (file_identity,),
                )
                if rows:
                    self._commit_rows(rows)
                committed = tuple(row[0] for row in rows)

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
        together. It never opens its own transaction.
        """
        self._conn.executemany(_INSERT_SQL, rows)

    def get_chunk_id_by_hash(self, content_hash: str) -> Optional[str]:
        """Find a chunk_id given a content_hash (from metadata)."""
        cursor = self._conn.execute(
            """SELECT chunk_id FROM chunks
               WHERE json_extract(metadata_json, '$.content_hash') = ?
               LIMIT 1""",
            (content_hash,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def get_all_file_paths(self) -> set[str]:
        """Return a set of all file paths currently in the repository."""
        cursor = self._conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE file_path != ''"
        )
        return {row[0] for row in cursor}

    def clear(self) -> None:
        """Remove all chunks and reset the FTS index."""
        with self._write_lock:
            with self._conn:
                self._conn.execute("DELETE FROM chunks")
                # Rebuild FTS index after bulk delete.
                self._conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"
                )

    def count(self) -> int:
        """Return the number of stored chunks."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM chunks")
        return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # FAISS index mapping
    # ------------------------------------------------------------------

    def get_faiss_idx(self, chunk_id: str) -> Optional[int]:
        """Return the rowid (used as FAISS index) for a chunk_id."""
        cursor = self._conn.execute(
            "SELECT rowid FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        )
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def get_chunk_id_by_faiss_idx(self, faiss_idx: int) -> Optional[str]:
        """Resolve a FAISS integer index back to a chunk_id."""
        cursor = self._conn.execute(
            "SELECT chunk_id FROM chunks WHERE rowid = ?",
            (faiss_idx,),
        )
        row = cursor.fetchone()
        return row[0] if row is not None else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception as e:
            logger.debug("Failed to close SQLite connection: %s", e)

    def save(self, path: Path) -> None:
        """Persist data (no-op as SQLite auto-persists)."""
        pass

    def load(self, path: Path) -> None:
        """Load database from the given directory path if different.

        Also handles automatic migration from legacy chunks.json if present.
        A fresh target initializes and validates the current schema.
        """
        db_path = path / "chunks.db"
        if db_path.resolve() != self._db_path.resolve():
            with self._write_lock:
                try:
                    self._conn.close()
                except Exception as e:
                    logger.debug("Failed to close connection during re-load: %s", e)
                self._db_path = db_path
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._init_schema()

    def get_all(self) -> list[CodeChunk]:
        """Fetch all chunks from the database (embeddings restored)."""
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM chunks"
        )
        return [self._row_to_chunk(row) for row in cursor]
