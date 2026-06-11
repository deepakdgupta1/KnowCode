"""Unit tests for SqliteChunkRepository.

TDD: These tests are written BEFORE the implementation.
They define the contract for the SQLite-backed chunk repository with FTS5 BM25.
"""

import sqlite3
import threading
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
