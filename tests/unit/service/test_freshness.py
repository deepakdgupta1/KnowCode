"""Unit tests for service freshness logic."""

from pathlib import Path
from unittest.mock import MagicMock

from knowcode.service import KnowCodeService
from knowcode.config import AppConfig


def test_service_get_freshness_metadata_returns_expected_structure(tmp_path: Path) -> None:
    """Test that get_freshness_metadata returns all expected keys."""
    # Write a dummy store file so it exists
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")

    # Create dummy source files
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print('hello')", encoding="utf-8")

    # Create dummy index manifest
    index_dir = tmp_path / "knowcode_index"
    index_dir.mkdir()
    (index_dir / "index_manifest.json").write_text("{}", encoding="utf-8")

    config = AppConfig()
    # Point store_path at tmp_path so it can resolve the store file and index path
    service = KnowCodeService(store_path=tmp_path, app_config=config)

    # Let's mock _store_root to be tmp_path
    service._store_root = MagicMock(return_value=tmp_path)
    service._store_file = MagicMock(return_value=store_file)
    service._index_path = MagicMock(return_value=index_dir)

    # Calling get_freshness_metadata (will fail because it is not implemented yet)
    meta = service.get_freshness_metadata()

    assert "last_store_rebuild" in meta
    assert "last_index_rebuild" in meta
    assert "latest_source_change" in meta
    assert "is_stale" in meta
    assert "stale_reasons" in meta


def test_retrieve_context_contains_freshness_signal(tmp_path: Path) -> None:
    """Test that retrieve_context_for_query results include freshness signal."""
    service = KnowCodeService(store_path=tmp_path)
    
    # Mock retrieve_context_for_query to return a dummy
    service._retrieval_orchestrator = MagicMock()
    service._retrieval_orchestrator.retrieve_context_for_query.return_value = {"context_text": "CTX"}
    
    # Mock get_freshness_metadata
    service.get_freshness_metadata = MagicMock(return_value={"is_stale": False})

    res = service.retrieve_context_for_query("test query")
    # Will fail if freshness is not merged
    assert "freshness" in res
    assert res["freshness"]["is_stale"] is False
