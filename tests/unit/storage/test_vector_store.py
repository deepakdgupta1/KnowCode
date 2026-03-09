"""Unit tests for vector store persistence."""

import json

import pytest

from knowcode.storage import vector_store
from knowcode.storage.vector_store import VectorStore


def test_vector_store_save_load(tmp_path) -> None:  # type: ignore
    """Vector store should persist index and ID map when FAISS is available."""
    if vector_store.faiss is None:  # type: ignore
        pytest.skip("faiss not installed")

    store = VectorStore(dimension=2)
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])

    path = tmp_path / "vectors"
    store.save(path)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == VectorStore.SCHEMA_VERSION

    loaded = VectorStore(dimension=2)
    loaded.load(path)
    assert loaded.id_map
    results = loaded.search([1.0, 0.0], limit=1)
    assert results[0][0] == "c1"


def test_vector_store_metadata_migrates_legacy_payload() -> None:
    """Metadata payloads without schema_version should migrate automatically."""
    migrated = VectorStore._validate_and_migrate_metadata({"id_map": {}, "dimension": 2})
    assert migrated["schema_version"] == VectorStore.SCHEMA_VERSION


def test_vector_store_metadata_migrates_schema_version_one() -> None:
    """Explicit schema_version=1 should migrate to current schema."""
    migrated = VectorStore._validate_and_migrate_metadata(
        {"schema_version": 1, "id_map": {}, "dimension": 2}
    )
    assert migrated["schema_version"] == VectorStore.SCHEMA_VERSION


def test_vector_store_metadata_rejects_unknown_schema() -> None:
    """Unsupported metadata schema versions should fail clearly."""
    with pytest.raises(ValueError, match="schema version"):
        VectorStore._validate_and_migrate_metadata({"schema_version": 999, "id_map": {}, "dimension": 2})
