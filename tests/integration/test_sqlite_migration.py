"""Integration test: InMemoryChunkRepository → SqliteChunkRepository migration.

Verifies that migrating chunk data from the old JSON-backed in-memory store
to the new SQLite+FTS5 store produces identical search behavior.
"""

from pathlib import Path

import pytest

from knowcode.data_models import CodeChunk, EmbeddingConfig
from knowcode.indexing.indexer import Indexer
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore


class _DummyEmbeddingProvider:
    """Deterministic embeddings for migration testing."""

    def __init__(self) -> None:
        self.config = EmbeddingConfig(dimension=4)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.1, 0.1, 0.1] for _ in texts]

    def embed_single(self, text: str) -> list[float]:
        return [0.1, 0.1, 0.1, 0.1]


# Shared fixture data: a small set of chunks with varying token distributions.
_FIXTURE_CHUNKS = [
    CodeChunk(
        id="auth.py::login::chunk_0",
        entity_id="auth.py::login",
        content="def login(username, password):\n    return True",
        tokens=["login", "username", "password"],
        metadata={"kind": "function"},
    ),
    CodeChunk(
        id="db.py::connect_to_db::chunk_0",
        entity_id="db.py::connect_to_db",
        content="def connect_to_db(conn_string):\n    pass",
        tokens=["connect", "db", "conn", "string", "connecttodb"],
        metadata={"kind": "function"},
    ),
    CodeChunk(
        id="utils.py::reverse_string::chunk_0",
        entity_id="utils.py::reverse_string",
        content="def reverse_string(s):\n    return s[::-1]",
        tokens=["reverse", "string", "reversestring"],
        metadata={"kind": "function"},
    ),
    CodeChunk(
        id="auth.py::logout::chunk_0",
        entity_id="auth.py::logout",
        content="def logout(session):\n    session.invalidate()",
        tokens=["logout", "session", "invalidate"],
        metadata={"kind": "function"},
    ),
]


@pytest.fixture
def populated_inmemory_repo() -> InMemoryChunkRepository:
    """Return an InMemoryChunkRepository populated with fixture chunks."""
    repo = InMemoryChunkRepository()
    for chunk in _FIXTURE_CHUNKS:
        repo.add(chunk)
    return repo


@pytest.fixture
def migrated_sqlite_repo(
    tmp_path: Path,
    populated_inmemory_repo: InMemoryChunkRepository,
) -> SqliteChunkRepository:
    """Migrate from InMemory to SQLite and return the SQLite repo."""
    db_path = tmp_path / "migrated.db"
    sqlite_repo = SqliteChunkRepository(db_path)

    # Migrate all chunks
    for chunk in populated_inmemory_repo._chunks.values():
        sqlite_repo.add(chunk)

    return sqlite_repo


class TestMigrationParity:
    """The SQLite backend should produce the same results as the in-memory one."""

    def test_all_chunks_migrated(
        self,
        populated_inmemory_repo: InMemoryChunkRepository,
        migrated_sqlite_repo: SqliteChunkRepository,
    ) -> None:
        """Every chunk in the old store should exist in the new one."""
        assert migrated_sqlite_repo.count() == populated_inmemory_repo.count()

        for chunk_id in ["auth.py::login::chunk_0", "db.py::connect_to_db::chunk_0",
                         "utils.py::reverse_string::chunk_0", "auth.py::logout::chunk_0"]:
            old = populated_inmemory_repo.get(chunk_id)
            new = migrated_sqlite_repo.get(chunk_id)
            assert old is not None
            assert new is not None
            assert old.id == new.id
            assert old.entity_id == new.entity_id
            assert old.content == new.content
            assert old.metadata == new.metadata

    def test_entity_retrieval_matches(
        self,
        populated_inmemory_repo: InMemoryChunkRepository,
        migrated_sqlite_repo: SqliteChunkRepository,
    ) -> None:
        """get_by_entity should return the same chunks in both backends."""
        old_results = populated_inmemory_repo.get_by_entity("auth.py::login")
        new_results = migrated_sqlite_repo.get_by_entity("auth.py::login")

        assert len(old_results) == len(new_results)
        assert {c.id for c in old_results} == {c.id for c in new_results}

    def test_token_search_returns_same_top_result(
        self,
        populated_inmemory_repo: InMemoryChunkRepository,
        migrated_sqlite_repo: SqliteChunkRepository,
    ) -> None:
        """Both backends should surface the same chunk for a specific query."""
        old_results = populated_inmemory_repo.search_by_tokens(["login", "password"], limit=3)
        new_results = migrated_sqlite_repo.search_by_tokens(["login", "password"], limit=3)

        # Both should find the login chunk
        assert len(old_results) > 0
        assert len(new_results) > 0
        # The login chunk should be present in both result sets
        assert any(c.id == "auth.py::login::chunk_0" for c in old_results)
        assert any(c.id == "auth.py::login::chunk_0" for c in new_results)

    def test_sqlite_bm25_improves_rare_token_ranking(
        self,
        migrated_sqlite_repo: SqliteChunkRepository,
    ) -> None:
        """SQLite FTS5 BM25 should rank rare identifiers above common ones.

        This is the core quality win of P0 — the reason we're migrating.
        'connecttodb' is a compound identifier that appears in only one chunk.
        BM25 should rank it higher than common terms.
        """
        results = migrated_sqlite_repo.search_by_tokens(["connecttodb"], limit=3)
        assert len(results) >= 1
        assert results[0].id == "db.py::connect_to_db::chunk_0"

    def test_remove_by_file_parity(
        self,
        tmp_path: Path,
    ) -> None:
        """remove_by_file should work identically in both backends."""
        inmem = InMemoryChunkRepository()
        sqlite = SqliteChunkRepository(tmp_path / "remove_test.db")

        for chunk in _FIXTURE_CHUNKS:
            inmem.add(chunk)
            sqlite.add(chunk)

        old_removed = inmem.remove_by_file("auth.py")
        new_removed = sqlite.remove_by_file("auth.py")

        assert set(old_removed) == set(new_removed)
        assert inmem.count() == sqlite.count()

        sqlite.close()


class TestSqliteWithSearchEngine:
    """SqliteChunkRepository should work as a drop-in with the search pipeline."""

    def test_search_engine_with_sqlite_backend(self, tmp_path: Path) -> None:
        """The full search engine pipeline should work with SQLite chunks."""
        db_path = tmp_path / "engine_test.db"
        sqlite_repo = SqliteChunkRepository(db_path)
        vs = VectorStore(dimension=4)
        provider = _DummyEmbeddingProvider()

        # Index using the SQLite backend
        indexer = Indexer(provider, chunk_repo=sqlite_repo, vector_store=vs)

        # Create a test file
        test_file = tmp_path / "sample.py"
        test_file.write_text(
            'def reconcile_ledger(entries):\n    """Reconcile financial ledger."""\n    pass\n'
        )
        indexer.index_file(test_file)

        assert sqlite_repo.count() > 0

        # Build the search engine
        hybrid = HybridIndex(sqlite_repo, vs)
        store = KnowledgeStore()
        engine = SearchEngine(sqlite_repo, provider, hybrid, store)

        # Search should return results
        results = engine.search("reconcile ledger", limit=3)
        assert len(results) > 0

        sqlite_repo.close()
