"""Tests for SQLite chunk repository wiring in KnowCodeService."""

from pathlib import Path
from knowcode.service import KnowCodeService
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

def test_get_indexer_wires_sqlite_by_default(tmp_path: Path) -> None:
    # Set up paths
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")
    
    # Initialize service
    service = KnowCodeService(store_path=tmp_path)
    
    # Retrieve indexer
    indexer = service.get_indexer()
    
    try:
        # Verify indexer chunk repo is SqliteChunkRepository
        assert isinstance(indexer.chunk_repo, SqliteChunkRepository)
        assert indexer.chunk_repo._db_path == tmp_path / "knowcode_index" / "chunks.db"
    finally:
        # Clean up
        indexer.chunk_repo.close()

def test_get_search_engine_wires_sqlite_by_default(tmp_path: Path) -> None:
    # Set up paths
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")
    
    # Initialize service
    service = KnowCodeService(store_path=tmp_path)
    
    # Retrieve search engine
    engine = service.get_search_engine()
    
    try:
        # Verify search engine chunk repo is SqliteChunkRepository
        assert isinstance(engine.chunk_repo, SqliteChunkRepository)
    finally:
        # Clean up
        engine.chunk_repo.close()
