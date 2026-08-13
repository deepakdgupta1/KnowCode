"""Unit tests for SqliteChunkRepository.

TDD: These tests are written BEFORE the implementation.
They define the contract for the SQLite-backed chunk repository with FTS5 BM25.
"""

import sqlite3
import threading
from array import array
from pathlib import Path
from typing import Generator

import pytest

from knowcode.data_models import CodeChunk

# Import will fail until we create the implementation — that's TDD.
# The test file defines expected behavior; implementation follows.
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a temporary database path."""
    return tmp_path / "test_chunks.db"


@pytest.fixture
def repo(db_path: Path) -> Generator[SqliteChunkRepository, None, None]:
    """Create a fresh SqliteChunkRepository for each test."""
    r = SqliteChunkRepository(db_path)
    yield r
    r.close()


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestBasicCRUD:
    """Chunk add/get/get_by_entity round-trip."""

    def test_add_and_get_by_id(self, repo: SqliteChunkRepository) -> None:
        """A chunk added to the repo should be retrievable by its ID."""
        chunk = CodeChunk(
            id="c1",
            entity_id="e1",
            content="def hello(): pass",
            tokens=["hello"],
            metadata={"kind": "function"},
        )
        repo.add(chunk)

        result = repo.get("c1")
        assert result is not None
        assert result.id == "c1"
        assert result.entity_id == "e1"
        assert result.content == "def hello(): pass"
        assert "hello" in result.tokens
        assert result.metadata["kind"] == "function"

    def test_get_nonexistent_returns_none(self, repo: SqliteChunkRepository) -> None:
        """Getting a chunk that doesn't exist should return None."""
        assert repo.get("nonexistent") is None

    def test_get_by_entity(self, repo: SqliteChunkRepository) -> None:
        """All chunks for an entity should be retrievable."""
        repo.add(CodeChunk(id="c1", entity_id="e1", content="part 1", tokens=["part"]))
        repo.add(CodeChunk(id="c2", entity_id="e1", content="part 2", tokens=["part"]))
        repo.add(CodeChunk(id="c3", entity_id="e2", content="other", tokens=["other"]))

        results = repo.get_by_entity("e1")
        assert len(results) == 2
        assert {r.id for r in results} == {"c1", "c2"}

    def test_get_by_entity_empty(self, repo: SqliteChunkRepository) -> None:
        """Getting chunks for a nonexistent entity should return empty list."""
        assert repo.get_by_entity("nonexistent") == []


# ---------------------------------------------------------------------------
# BM25 Search Quality (the core P0 win)
# ---------------------------------------------------------------------------


class TestBM25Search:
    """FTS5-backed BM25 search should rank rare tokens higher than common ones."""

    def test_search_by_tokens_bm25_ranking(
        self, repo: SqliteChunkRepository
    ) -> None:
        """Rare identifiers should rank higher than common tokens like 'self'.

        This is the critical quality test — the whole reason P0 exists.
        With set-intersection (old behavior), 'self' and 'reconcile_ledger'
        have equal weight. With BM25/IDF, 'reconcile_ledger' dominates
        because it appears in fewer documents.
        """
        # Add many chunks with common token 'self'
        for i in range(20):
            repo.add(
                CodeChunk(
                    id=f"common_{i}",
                    entity_id=f"e_common_{i}",
                    content=f"def method_{i}(self): self.value = {i}",
                    tokens=["self", "value", f"method{i}"],
                )
            )

        # Add one chunk with a rare identifier
        repo.add(
            CodeChunk(
                id="rare_chunk",
                entity_id="e_rare",
                content="def reconcile_ledger(self, entries): self.reconcile(entries)",
                tokens=["reconcile", "ledger", "self", "entries", "reconcileledger"],
            )
        )

        # Search for the rare identifier — it should be ranked first
        results = repo.search_by_tokens(["reconcile", "ledger"], limit=5)
        assert len(results) > 0
        assert results[0].id == "rare_chunk"

    def test_search_by_tokens_respects_limit(
        self, repo: SqliteChunkRepository
    ) -> None:
        """Token search should respect the limit parameter."""
        for i in range(10):
            repo.add(
                CodeChunk(
                    id=f"c_{i}",
                    entity_id=f"e_{i}",
                    content=f"alpha beta gamma {i}",
                    tokens=["alpha", "beta", "gamma"],
                )
            )

        results = repo.search_by_tokens(["alpha"], limit=3)
        assert len(results) == 3

    def test_search_by_tokens_no_match(self, repo: SqliteChunkRepository) -> None:
        """Searching for tokens that don't exist should return empty list."""
        repo.add(
            CodeChunk(
                id="c1",
                entity_id="e1",
                content="hello world",
                tokens=["hello", "world"],
            )
        )
        results = repo.search_by_tokens(["nonexistent", "tokens"], limit=10)
        assert results == []

    def test_compound_token_indexing(self, repo: SqliteChunkRepository) -> None:
        """Searching for a compound identifier should match chunks containing it.

        The tokenizer produces both subtokens AND the joined compound:
        getUserById -> ['get', 'user', 'by', 'id', 'getuserbyid']
        Searching for 'getuserbyid' should find the chunk.
        """
        repo.add(
            CodeChunk(
                id="c_compound",
                entity_id="e_compound",
                content="def getUserById(self, user_id): pass",
                tokens=["get", "user", "by", "id", "getuserbyid"],
            )
        )
        repo.add(
            CodeChunk(
                id="c_other",
                entity_id="e_other",
                content="def get_something(): pass",
                tokens=["get", "something"],
            )
        )

        results = repo.search_by_tokens(["getuserbyid"], limit=5)
        assert len(results) >= 1
        assert results[0].id == "c_compound"


# ---------------------------------------------------------------------------
# File removal
# ---------------------------------------------------------------------------


class TestFileRemoval:
    """remove_by_file should remove all chunks associated with a file path."""

    def test_remove_by_file(self, repo: SqliteChunkRepository) -> None:
        """Removing by file path should delete matching chunks and return IDs."""
        repo.add(
            CodeChunk(
                id="c1",
                entity_id="/path/to/file.py::MyClass",
                content="class MyClass: pass",
                tokens=["myclass"],
            )
        )
        repo.add(
            CodeChunk(
                id="c2",
                entity_id="/path/to/file.py::MyClass::method",
                content="def method(self): pass",
                tokens=["method", "self"],
            )
        )
        repo.add(
            CodeChunk(
                id="c3",
                entity_id="/path/to/other.py::OtherClass",
                content="class OtherClass: pass",
                tokens=["otherclass"],
            )
        )

        removed = repo.remove_by_file("/path/to/file.py")
        assert set(removed) == {"c1", "c2"}

        # Verify they're gone
        assert repo.get("c1") is None
        assert repo.get("c2") is None
        # Verify unrelated chunk still exists
        assert repo.get("c3") is not None

    def test_remove_by_file_no_match(self, repo: SqliteChunkRepository) -> None:
        """Removing by a file path with no matches should return empty list."""
        repo.add(
            CodeChunk(
                id="c1",
                entity_id="e1",
                content="hello",
                tokens=["hello"],
            )
        )
        removed = repo.remove_by_file("/nonexistent/path.py")
        assert removed == []


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    """clear() should remove all chunks."""

    def test_clear(self, repo: SqliteChunkRepository) -> None:
        """After clear(), the repository should be empty."""
        repo.add(CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"]))
        repo.add(CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"]))

        assert repo.count() == 2
        repo.clear()
        assert repo.count() == 0
        assert repo.get("c1") is None
        assert repo.get("c2") is None


# ---------------------------------------------------------------------------
# Batch add
# ---------------------------------------------------------------------------


class TestBatchAdd:
    """add_batch() should insert multiple chunks in one transaction."""

    def test_batch_add(self, repo: SqliteChunkRepository) -> None:
        """Batch-adding chunks should make all of them retrievable."""
        chunks = [
            CodeChunk(
                id=f"batch_{i}",
                entity_id=f"e_{i}",
                content=f"content {i}",
                tokens=[f"token{i}"],
            )
            for i in range(50)
        ]

        repo.add_batch(chunks)

        assert repo.count() == 50
        for i in range(50):
            result = repo.get(f"batch_{i}")
            assert result is not None
            assert result.content == f"content {i}"


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


class TestCount:
    """count() should return the number of stored chunks."""

    def test_count_empty(self, repo: SqliteChunkRepository) -> None:
        assert repo.count() == 0

    def test_count_after_add(self, repo: SqliteChunkRepository) -> None:
        repo.add(CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"]))
        assert repo.count() == 1


# ---------------------------------------------------------------------------
# Persistence (save / load round-trip)
# ---------------------------------------------------------------------------


class TestPersistence:
    """The SQLite DB should survive close/reopen."""

    def test_save_load_roundtrip(self, db_path: Path) -> None:
        """Data persists across close/reopen of the repository."""
        # Write
        repo1 = SqliteChunkRepository(db_path)
        repo1.add(
            CodeChunk(
                id="persist_c1",
                entity_id="persist_e1",
                content="persistent content",
                tokens=["persistent", "content"],
                metadata={"key": "value"},
            )
        )
        repo1.close()

        # Read back
        repo2 = SqliteChunkRepository(db_path)
        result = repo2.get("persist_c1")
        assert result is not None
        assert result.content == "persistent content"
        assert result.metadata["key"] == "value"

        # FTS5 search should also work after reload
        search_results = repo2.search_by_tokens(["persistent"], limit=5)
        assert len(search_results) == 1
        assert search_results[0].id == "persist_c1"
        repo2.close()


# ---------------------------------------------------------------------------
# FAISS index mapping
# ---------------------------------------------------------------------------


class TestFaissIdxMapping:
    """faiss_idx column should provide integer FAISS ID mapping."""

    def test_faiss_idx_assignment(self, repo: SqliteChunkRepository) -> None:
        """Each added chunk should get a sequential faiss_idx."""
        repo.add(CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"]))
        repo.add(CodeChunk(id="c2", entity_id="e2", content="b", tokens=["b"]))

        idx1 = repo.get_faiss_idx("c1")
        idx2 = repo.get_faiss_idx("c2")
        assert idx1 is not None
        assert idx2 is not None
        assert isinstance(idx1, int)
        assert isinstance(idx2, int)
        assert idx1 != idx2

    def test_chunk_id_from_faiss_idx(self, repo: SqliteChunkRepository) -> None:
        """Should be able to resolve faiss_idx back to chunk_id."""
        repo.add(CodeChunk(id="c1", entity_id="e1", content="a", tokens=["a"]))

        idx = repo.get_faiss_idx("c1")
        assert idx is not None
        chunk_id = repo.get_chunk_id_by_faiss_idx(idx)
        assert chunk_id == "c1"


# ---------------------------------------------------------------------------
# Concurrent access (WAL mode)
# ---------------------------------------------------------------------------


class TestConcurrency:
    """WAL mode should support concurrent reads + single writer."""

    def test_concurrent_read_under_wal(self, db_path: Path) -> None:
        """Two readers and one writer should not deadlock under WAL mode."""
        repo = SqliteChunkRepository(db_path)
        # Seed data
        for i in range(100):
            repo.add(
                CodeChunk(
                    id=f"conc_{i}",
                    entity_id=f"e_{i}",
                    content=f"concurrent content {i}",
                    tokens=["concurrent", "content", f"token{i}"],
                )
            )

        errors: list[str] = []
        results: list[int] = []

        def reader(reader_id: int) -> None:
            try:
                # Open a separate connection for the reader
                read_repo = SqliteChunkRepository(db_path)
                for j in range(10):
                    r = read_repo.search_by_tokens(["concurrent"], limit=5)
                    results.append(len(r))
                read_repo.close()
            except Exception as exc:
                errors.append(f"Reader {reader_id}: {exc}")

        def writer() -> None:
            try:
                write_repo = SqliteChunkRepository(db_path)
                for j in range(10):
                    write_repo.add(
                        CodeChunk(
                            id=f"writer_{j}",
                            entity_id=f"e_writer_{j}",
                            content=f"written during concurrency test {j}",
                            tokens=["written", "concurrency"],
                        )
                    )
                write_repo.close()
            except Exception as exc:
                errors.append(f"Writer: {exc}")

        threads = [
            threading.Thread(target=reader, args=(1,)),
            threading.Thread(target=reader, args=(2,)),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        repo.close()

        assert not errors, f"Concurrency errors: {errors}"
        assert len(results) == 20  # 2 readers × 10 reads each


# ---------------------------------------------------------------------------
# Durable embedding persistence (Step 08 — ADR 3)
# ---------------------------------------------------------------------------


def _float32(values: list[float]) -> list[float]:
    """Return the values exactly as little-endian float32 round-trips them."""
    arr = array("f", values)
    if __import__("sys").byteorder == "big":
        arr.byteswap()
    decoded = array("f")
    decoded.frombytes(arr.tobytes())
    if __import__("sys").byteorder == "big":
        decoded.byteswap()
    return list(decoded)


class TestEmbeddingPersistence:
    """Embeddings must survive as validated durable float32 BLOBs (ADR 3)."""

    def test_embedding_round_trips_through_reload(self, db_path: Path) -> None:
        """A stored embedding must be byte-identical after close/reopen."""
        embedding = [1.0, 0.5, -0.25, 0.0, 0.125]
        repo = SqliteChunkRepository(db_path)
        repo.add(
            CodeChunk(
                id="emb_c1",
                entity_id="/src/app.py::foo",
                content="def foo(): pass",
                tokens=["foo"],
                embedding=embedding,
            )
        )
        repo.close()

        reopened = SqliteChunkRepository(db_path)
        chunk = reopened.get("emb_c1")
        reopened.close()

        assert chunk is not None
        assert chunk.embedding is not None
        assert chunk.embedding == _float32(embedding)

    def test_embedding_is_none_when_absent(self, repo: SqliteChunkRepository) -> None:
        """A chunk stored without an embedding must load with embedding None."""
        repo.add(
            CodeChunk(
                id="no_emb",
                entity_id="/src/app.py::bar",
                content="def bar(): pass",
                tokens=["bar"],
            )
        )
        chunk = repo.get("no_emb")
        assert chunk is not None
        assert chunk.embedding is None

    def test_get_all_restores_embeddings(self, repo: SqliteChunkRepository) -> None:
        """Durable embedding iteration (get_all) must restore embeddings."""
        repo.add(
            CodeChunk(
                id="a",
                entity_id="/src/a.py::a",
                content="a",
                tokens=["a"],
                embedding=[0.25, 0.75],
            )
        )
        repo.add(
            CodeChunk(
                id="b",
                entity_id="/src/b.py::b",
                content="b",
                tokens=["b"],
            )
        )
        chunks = {c.id: c for c in repo.get_all()}
        assert chunks["a"].embedding == _float32([0.25, 0.75])
        assert chunks["b"].embedding is None

    def test_embedding_rejects_non_finite_values(
        self, repo: SqliteChunkRepository
    ) -> None:
        """NaN/inf are not valid durable embeddings."""
        with pytest.raises((ValueError, OverflowError)):
            repo.add(
                CodeChunk(
                    id="nan",
                    entity_id="/src/n.py::n",
                    content="n",
                    tokens=["n"],
                    embedding=[float("nan"), 1.0],
                )
            )

    def test_configured_dimension_mismatch_rejected(self, tmp_path: Path) -> None:
        """When a dimension is configured, a mismatched embedding fails closed."""
        repo = SqliteChunkRepository(tmp_path / "dim.db", dimension=4)
        try:
            with pytest.raises(ValueError):
                repo.add(
                    CodeChunk(
                        id="bad_dim",
                        entity_id="/src/x.py::x",
                        content="x",
                        tokens=["x"],
                        embedding=[1.0, 2.0, 3.0],
                    )
                )
        finally:
            repo.close()


# ---------------------------------------------------------------------------
# Atomic file replacement (Step 08 — ADR 7)
# ---------------------------------------------------------------------------


def _chunk(chunk_id: str, entity: str, content: str, token: str) -> CodeChunk:
    return CodeChunk(id=chunk_id, entity_id=entity, content=content, tokens=[token])


class TestReplaceFile:
    """replace_file must be an all-or-nothing transactional replacement."""

    def test_replace_file_swaps_chunks_and_reports_ids(
        self, repo: SqliteChunkRepository
    ) -> None:
        file_path = "/src/widget.py"
        repo.add(_chunk("old1", f"{file_path}::Widget", "v1 body", "widget"))
        repo.add(_chunk("old2", f"{file_path}::Widget::render", "v1 render", "render"))

        replacement = [
            _chunk("new1", f"{file_path}::Widget", "v2 body", "widget"),
            _chunk("new2", f"{file_path}::Widget::render", "v2 render", "render"),
            _chunk("new3", f"{file_path}::Widget::mounted", "v2 mounted", "mounted"),
        ]

        result = repo.replace_file(file_path, replacement)

        assert set(result.previous_chunk_ids) == {"old1", "old2"}
        assert set(result.committed_chunk_ids) == {"new1", "new2", "new3"}

        # Old chunks are gone, new chunks are present.
        assert repo.get("old1") is None and repo.get("old2") is None
        assert {c.id for c in repo.get_by_entity(f"{file_path}::Widget")} == {"new1"}

    def test_replace_file_empty_list_removes_all(
        self, repo: SqliteChunkRepository
    ) -> None:
        file_path = "/src/gone.py"
        repo.add(_chunk("g1", f"{file_path}::Gone", "body", "gone"))

        result = repo.replace_file(file_path, [])

        assert set(result.previous_chunk_ids) == {"g1"}
        assert result.committed_chunk_ids == ()
        assert repo.get("g1") is None
        assert file_path not in repo.get_all_file_paths()

    def test_replace_file_reports_generation_metadata(
        self, repo: SqliteChunkRepository
    ) -> None:
        file_path = "/src/meta.py"
        repo.add(_chunk("m1", f"{file_path}::Meta", "a", "meta"))

        result = repo.replace_file(
            file_path,
            [_chunk("m2", f"{file_path}::Meta", "b", "meta")],
        )

        metadata = result.generation_metadata
        assert isinstance(metadata, dict)
        assert metadata["file_path"] == file_path
        assert metadata["previous_count"] == 1
        assert metadata["committed_count"] == 1
        # A monotonic, comparable generation stamp is recorded.
        assert isinstance(metadata["generation"], int)

    def test_replace_file_rolls_back_on_injected_failure(
        self, repo: SqliteChunkRepository
    ) -> None:
        """A failure mid-replacement must leave the previous generation intact."""
        file_path = "/src/rollback.py"
        repo.add(_chunk("keep1", f"{file_path}::Keep", "original", "keep"))

        # Inject the failure on the in-transaction commit seam, after the
        # in-transaction DELETE has already run. The whole transaction must
        # roll back so the previous chunk stays searchable.
        original_commit = repo._commit_rows

        def failing_commit(rows):
            raise sqlite3.OperationalError("injected failure during replace")

        repo._commit_rows = failing_commit  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                repo.replace_file(
                    file_path,
                    [_chunk("dead1", f"{file_path}::Keep", "replacement", "keep")],
                )
        finally:
            repo._commit_rows = original_commit  # type: ignore[assignment]

        # The previous chunk must still be present and searchable.
        assert repo.get("keep1") is not None
        assert repo.get("dead1") is None
        assert [c.id for c in repo.search_by_tokens(["keep"], limit=5)] == ["keep1"]

    def test_replace_file_leaves_other_files_intact(
        self, repo: SqliteChunkRepository
    ) -> None:
        repo.add(_chunk("other", "/src/other.py::Other", "other body", "other"))
        repo.replace_file(
            "/src/target.py",
            [_chunk("t1", "/src/target.py::Target", "target body", "target")],
        )
        assert repo.get("other") is not None

    def test_replace_file_keeps_fts_index_consistent(
        self, repo: SqliteChunkRepository
    ) -> None:
        file_path = "/src/fts.py"
        repo.add(_chunk("fts_old", f"{file_path}::Fts", "alpha", "alpha"))

        repo.replace_file(
            file_path,
            [_chunk("fts_new", f"{file_path}::Fts", "beta", "beta")],
        )

        assert [c.id for c in repo.search_by_tokens(["alpha"], limit=5)] == []
        assert [c.id for c in repo.search_by_tokens(["beta"], limit=5)] == ["fts_new"]


# ---------------------------------------------------------------------------
# Normalized path identity (Step 08 — ADR 1)
# ---------------------------------------------------------------------------


class TestPathNormalization:
    """Replacement and removal must key on canonical file identity."""

    @staticmethod
    def _aliased_str(target: Path) -> str:
        """Return a raw string alias that resolve() collapses to ``target``.

        A ``..`` segment is preserved verbatim by pathlib string construction
        but normalised away by ``Path.resolve()``, so it exercises the
        canonical-identity step rather than being folded before it reaches the
        repository (a bare ``.`` is collapsed too early to be a real alias).
        """
        return f"{target.parent}/sub/../{target.name}"

    def test_replace_file_normalizes_path_alias(
        self, repo: SqliteChunkRepository, tmp_path: Path
    ) -> None:
        target = tmp_path / "real.py"
        target.write_text("x = 1\n", encoding="utf-8")

        repo.replace_file(
            str(target),
            [_chunk("p1", f"{target.resolve().as_posix()}::X", "body", "token")],
        )

        # Replacing via the ``..`` alias must hit the same canonical rows.
        result = repo.replace_file(self._aliased_str(target), [])
        assert set(result.previous_chunk_ids) == {"p1"}

    def test_remove_by_file_normalizes_path_alias(
        self, repo: SqliteChunkRepository, tmp_path: Path
    ) -> None:
        target = tmp_path / "module.py"
        target.write_text("x = 1\n", encoding="utf-8")

        repo.add(_chunk("r1", f"{target.resolve().as_posix()}::M", "body", "token"))

        removed = repo.remove_by_file(self._aliased_str(target))
        assert set(removed) == {"r1"}


# ---------------------------------------------------------------------------
# Schema versioning and fail-closed migration (Step 08 — ADR 3/7)
# ---------------------------------------------------------------------------


class TestSchemaVersioning:
    """The schema must record its version and fail closed on legacy v1."""

    def test_fresh_db_records_current_schema_version(
        self, repo: SqliteChunkRepository
    ) -> None:
        assert repo.schema_version == SqliteChunkRepository.SCHEMA_VERSION
        # schema_meta table is the source of truth on disk.
        row = repo._conn.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()
        assert row is not None and row[0] == SqliteChunkRepository.SCHEMA_VERSION

    def test_chunks_table_has_embedding_columns(
        self, repo: SqliteChunkRepository
    ) -> None:
        columns = {
            row[1]
            for row in repo._conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        assert "embedding" in columns
        assert "embedding_dim" in columns

    def test_legacy_v1_schema_fails_closed(self, tmp_path: Path) -> None:
        """A chunks.db without the embedding column must not be silently used."""
        legacy_path = tmp_path / "legacy.db"
        # Hand-build the baseline v1 schema (no embedding column, no schema_meta).
        conn = sqlite3.connect(str(legacy_path))
        conn.execute(
            "CREATE TABLE chunks (chunk_id TEXT UNIQUE, entity_id TEXT, "
            "content TEXT, tokens_text TEXT, metadata_json TEXT, file_path TEXT)"
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
            "metadata_json, file_path) VALUES ('legacy1', '/x.py::X', 'c', 't', '{}', '/x.py')"
        )
        conn.commit()
        conn.close()

        with pytest.raises(Exception) as excinfo:  # noqa: PT011 — intentional broad
            SqliteChunkRepository(legacy_path)

        message = str(excinfo.value).lower()
        assert "rebuild" in message or "migration" in message

    def test_load_initializes_schema_for_fresh_target(
        self, tmp_path: Path
    ) -> None:
        """load() into a directory without chunks.db must initialize the schema."""
        repo = SqliteChunkRepository(tmp_path / "start.db")
        repo.load(tmp_path / "fresh_dir")
        try:
            assert repo.schema_version == SqliteChunkRepository.SCHEMA_VERSION
            row = repo._conn.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()
            assert row is not None
        finally:
            repo.close()


# ---------------------------------------------------------------------------
# Decode validation and edge cases (ADR 3 — load must validate)
# ---------------------------------------------------------------------------


class TestEmbeddingAndMetadataValidation:
    """Load-side validation of corrupted embeddings and metadata."""

    def test_decode_rejects_byte_length_dim_mismatch(
        self, repo: SqliteChunkRepository
    ) -> None:
        """A BLOB whose byte length disagrees with its stored dimension fails."""
        repo.add(
            CodeChunk(
                id="corrupt",
                entity_id="/src/c.py::C",
                content="c",
                tokens=["c"],
                embedding=[0.5, 0.5],
            )
        )
        # Corrupt the stored dimension so byte length no longer matches.
        repo._conn.execute(
            "UPDATE chunks SET embedding_dim = 3 WHERE chunk_id = ?", ("corrupt",)
        )
        repo._conn.commit()
        with pytest.raises(ValueError):
            repo.get("corrupt")

    def test_decode_rejects_non_multiple_of_four_blob(
        self, repo: SqliteChunkRepository
    ) -> None:
        """A truncated BLOB that is not a multiple of four bytes fails."""
        repo._conn.execute(
            "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
            "metadata_json, file_path, embedding, embedding_dim) "
            "VALUES ('odd', '/x.py::X', 'c', 't', '{}', '/x.py', X'0102', NULL)"
        )
        repo._conn.commit()
        with pytest.raises(ValueError):
            repo.get("odd")

    def test_corrupt_metadata_falls_back_to_empty_dict(
        self, repo: SqliteChunkRepository
    ) -> None:
        """Invalid or non-dict metadata must not break row hydration."""
        repo._conn.execute(
            "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
            "metadata_json, file_path, embedding, embedding_dim) "
            "VALUES ('badmeta', '/x.py::X', 'c', 't', 'not-json', '/x.py', NULL, NULL)"
        )
        repo._conn.commit()
        chunk = repo.get("badmeta")
        assert chunk is not None
        assert chunk.metadata == {}

    def test_decode_embedding_without_stored_dim(
        self, repo: SqliteChunkRepository
    ) -> None:
        """A valid BLOB with a null dimension still decodes by byte length."""
        blob, _ = repo._encode_embedding([0.25, 0.75, 1.0])
        repo._conn.execute(
            "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
            "metadata_json, file_path, embedding, embedding_dim) "
            "VALUES ('nodim', '/x.py::X', 'c', 't', '{}', '/x.py', ?, NULL)",
            (blob,),
        )
        repo._conn.commit()
        chunk = repo.get("nodim")
        assert chunk is not None
        assert chunk.embedding is not None
        assert len(chunk.embedding) == 3


# ---------------------------------------------------------------------------
# Empty-input edge cases
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    """add_batch, search, and exact search must handle empty input cleanly."""

    def test_add_batch_empty_is_a_noop(self, repo: SqliteChunkRepository) -> None:
        repo.add_batch([])
        assert repo.count() == 0

    def test_search_by_tokens_empty_returns_empty(
        self, repo: SqliteChunkRepository
    ) -> None:
        assert repo.search_by_tokens([], limit=5) == []

    def test_search_by_tokens_all_punctuation_returns_empty(
        self, repo: SqliteChunkRepository
    ) -> None:
        repo.add(
            CodeChunk(id="c1", entity_id="/x.py::X", content="body", tokens=["body"])
        )
        assert repo.search_by_tokens(["!!!", "@@@"], limit=5) == []

    def test_search_exact_empty_returns_empty(
        self, repo: SqliteChunkRepository
    ) -> None:
        assert repo.search_exact("", limit=5) == []
