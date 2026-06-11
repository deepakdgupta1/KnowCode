"""SQLite-backed chunk repository with FTS5 BM25 search.

Replaces the in-memory flat-file design with a single SQLite database:
- Real BM25 via FTS5's built-in ``bm25()`` ranking (fixes G3, G4).
- Lazy row-level access — no O(n) startup hydration (fixes G2, G5).
- WAL mode for concurrent reads + single writer (fixes G6).
- Dense integer IDs via ``faiss_idx`` column (fixes G8).

See: docs/research/knowcode-architecture-synthesis.md §3.1
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from knowcode.data_models import CodeChunk
from knowcode.storage.chunk_repository import ChunkRepository
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


class SqliteChunkRepository(ChunkRepository):
    """SQLite + FTS5 implementation of ChunkRepository.

    Uses WAL mode for safe concurrent reads with a single writer.
    The FTS5 virtual table provides real BM25 ranking for sparse search.
    """

    _SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path) -> None:
        """Open or create the SQLite chunk database.

        Args:
            db_path: Path to the ``.db`` file.  Created if it does not exist.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._write_lock = threading.Lock()

        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not already exist."""
        with self._conn:
            # Enable WAL mode for concurrent readers + single writer.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id   TEXT    NOT NULL UNIQUE,
                    entity_id  TEXT    NOT NULL,
                    content    TEXT    NOT NULL DEFAULT '',
                    tokens_text TEXT   NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    file_path  TEXT    NOT NULL DEFAULT ''
                )
            """)

            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_entity_id
                ON chunks (entity_id)
            """)

            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_file_path
                ON chunks (file_path)
            """)

            # FTS5 external-content table backed by chunks.tokens_text.
            # Using unicode61 tokenizer — our Python tokenizer already
            # handles camelCase/snake_case splitting, so FTS5 only needs
            # to split on whitespace.
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(
                    tokens_text,
                    content='chunks',
                    content_rowid='rowid',
                    tokenize='unicode61'
                )
            """)

            # Triggers to keep the FTS index in sync with the content table.
            self._conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, tokens_text)
                    VALUES (new.rowid, new.tokens_text);
                END;

                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, tokens_text)
                    VALUES ('delete', old.rowid, old.tokens_text);
                END;

                CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, tokens_text)
                    VALUES ('delete', old.rowid, old.tokens_text);
                    INSERT INTO chunks_fts(rowid, tokens_text)
                    VALUES (new.rowid, new.tokens_text);
                END;
            """)

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

    def _row_to_chunk(self, row: tuple) -> CodeChunk:  # type: ignore[type-arg]
        """Convert a database row to a CodeChunk.

        Expected column order:
            chunk_id, entity_id, content, tokens_text, metadata_json
        """
        chunk_id, entity_id, content, tokens_text, metadata_json = row
        tokens = tokens_text.split() if tokens_text else []
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return CodeChunk(
            id=chunk_id,
            entity_id=entity_id,
            content=content,
            tokens=tokens,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # ChunkRepository interface
    # ------------------------------------------------------------------

    def add(self, chunk: CodeChunk) -> None:
        """Insert or replace a single chunk."""
        tokens_text = " ".join(chunk.tokens) if chunk.tokens else ""
        metadata_json = json.dumps(chunk.metadata) if chunk.metadata else "{}"
        file_path = self._file_path_from_entity_id(chunk.entity_id)

        with self._write_lock:
            with self._conn:
                self._conn.execute(
                    """INSERT OR REPLACE INTO chunks
                       (chunk_id, entity_id, content, tokens_text, metadata_json, file_path)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk.id, chunk.entity_id, chunk.content,
                     tokens_text, metadata_json, file_path),
                )

    def add_batch(self, chunks: list[CodeChunk]) -> None:
        """Insert multiple chunks in a single transaction."""
        if not chunks:
            return

        rows = []
        for chunk in chunks:
            tokens_text = " ".join(chunk.tokens) if chunk.tokens else ""
            metadata_json = json.dumps(chunk.metadata) if chunk.metadata else "{}"
            file_path = self._file_path_from_entity_id(chunk.entity_id)
            rows.append((
                chunk.id, chunk.entity_id, chunk.content,
                tokens_text, metadata_json, file_path,
            ))

        with self._write_lock:
            with self._conn:
                self._conn.executemany(
                    """INSERT OR REPLACE INTO chunks
                       (chunk_id, entity_id, content, tokens_text, metadata_json, file_path)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    rows,
                )

    def get(self, chunk_id: str) -> Optional[CodeChunk]:
        """Fetch a single chunk by ID (lazy, single-row SELECT)."""
        cursor = self._conn.execute(
            """SELECT chunk_id, entity_id, content, tokens_text, metadata_json
               FROM chunks WHERE chunk_id = ?""",
            (chunk_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def get_by_entity(self, entity_id: str) -> list[CodeChunk]:
        """Fetch all chunks belonging to an entity."""
        cursor = self._conn.execute(
            """SELECT chunk_id, entity_id, content, tokens_text, metadata_json
               FROM chunks WHERE entity_id = ?""",
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
                """SELECT c.chunk_id, c.entity_id, c.content,
                          c.tokens_text, c.metadata_json
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

    def remove_by_file(self, file_path: str) -> list[str]:
        """Remove all chunks whose entity_id starts with ``file_path``.

        Returns the list of removed chunk IDs.
        """
        with self._write_lock:
            # Identify chunks to remove.
            cursor = self._conn.execute(
                """SELECT chunk_id FROM chunks
                   WHERE entity_id = ? OR entity_id LIKE ?""",
                (file_path, file_path + "::%"),
            )
            removed_ids = [row[0] for row in cursor]

            if removed_ids:
                with self._conn:
                    self._conn.execute(
                        """DELETE FROM chunks
                           WHERE entity_id = ? OR entity_id LIKE ?""",
                        (file_path, file_path + "::%"),
                    )

        return removed_ids

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
        return cursor.fetchone()[0]  # type: ignore[index]

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
        """
        db_path = path / "chunks.db"
        if db_path.resolve() != self._db_path.resolve():
            with self._write_lock:
                try:
                    self._conn.close()
                except Exception as e:
                    logger.debug("Failed to close connection during re-load: %s", e)
                self._db_path = db_path
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")

        # Automatic migration from chunks.json if the database is empty and chunks.json exists
        chunks_json = path / "chunks.json"
        if chunks_json.exists() and self.count() == 0:
            logger.info("Migrating legacy chunks.json to SQLite database...")
            import json
            try:
                with open(chunks_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error("Failed to read chunks.json for migration: %s", e)
                return

            if isinstance(data, dict):
                # Validate schema version
                schema_version = data.get("schema_version")
                if schema_version is not None:
                    norm = None
                    if isinstance(schema_version, int) and not isinstance(schema_version, bool):
                        norm = schema_version
                    elif isinstance(schema_version, str) and schema_version.isdigit():
                        norm = int(schema_version)
                    if norm not in (1, 2):
                        raise ValueError(
                            f"Unsupported chunks metadata schema version {schema_version!r}. "
                            "Supported versions: [2]. Rebuild with `knowcode build`."
                        )

                chunk_entries = data.get("chunks", [])
                if isinstance(chunk_entries, list):
                    chunks_to_add = []
                    for c_data in chunk_entries:
                        if not isinstance(c_data, dict):
                            continue
                        chunks_to_add.append(CodeChunk(
                            id=c_data.get("id", ""),
                            entity_id=c_data.get("entity_id", ""),
                            content=c_data.get("content", ""),
                            tokens=c_data.get("tokens", []),
                            metadata=c_data.get("metadata", {}),
                        ))
                    if chunks_to_add:
                        self.add_batch(chunks_to_add)

    def get_all(self) -> list[CodeChunk]:
        """Fetch all chunks from the database."""
        cursor = self._conn.execute(
            """SELECT chunk_id, entity_id, content, tokens_text, metadata_json
               FROM chunks"""
        )
        return [self._row_to_chunk(row) for row in cursor]
