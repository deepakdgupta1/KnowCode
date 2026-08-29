"""Tests for vector backend inspection and factory behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowcode.storage.lancedb_vector_store import LanceDBVectorStore
from knowcode.storage.vector_backends import (
    create_vector_store,
    inspect_vector_index,
)
from knowcode.storage.vector_store import VectorStore


def _write_vectors_json(path: Path, payload: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "vectors.json").write_text(json.dumps(payload), encoding="utf-8")


def test_inspect_lancedb_vector_artifact(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {
            "schema_version": LanceDBVectorStore.SCHEMA_VERSION,
            "dimension": 1024,
            "backend": "lancedb",
        },
    )
    (tmp_path / "vectors.lancedb").mkdir()

    inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert inspection.ok
    assert inspection.backend == "lancedb"
    assert inspection.warnings == ()


def test_inspect_faiss_vector_artifact(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {
            "schema_version": VectorStore.SCHEMA_VERSION,
            "dimension": 1024,
            "id_map": {},
        },
    )
    (tmp_path / "vectors.index").write_bytes(b"placeholder")

    inspection = inspect_vector_index(tmp_path, configured_backend="faiss")

    assert inspection.ok
    assert inspection.backend == "faiss"
    assert inspection.warnings == ()


def test_inspect_numpy_fallback_vector_artifact(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {
            "schema_version": VectorStore.SCHEMA_VERSION,
            "dimension": 1024,
            "id_map": {},
        },
    )
    (tmp_path / "vectors.npy").write_bytes(b"placeholder")

    inspection = inspect_vector_index(tmp_path, configured_backend="faiss")

    assert inspection.ok
    assert inspection.backend == "faiss"


def test_inspect_missing_vector_artifact_fails(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {
            "schema_version": LanceDBVectorStore.SCHEMA_VERSION,
            "dimension": 1024,
            "backend": "lancedb",
        },
    )

    inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert not inspection.ok
    assert "missing vectors.lancedb" in inspection.failures


def test_inspect_unknown_vector_backend_fails(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {"schema_version": 1, "dimension": 1024, "backend": "weaviate"},
    )

    inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert not inspection.ok
    assert "unsupported vector backend: 'weaviate'" in inspection.failures


def test_schema_check_uses_selected_backend(tmp_path: Path) -> None:
    """A legacy envelope fails closed on both backends, with its own version.

    Step 11 stopped migrating v1/v2 FAISS metadata in memory and Step 12 did the
    same for LanceDB v1: neither artifact can be verified against its index, so
    doctor reports an actionable failure instead of a migration warning. Each
    backend is checked against its *own* current version.
    """
    _write_vectors_json(
        tmp_path,
        {"schema_version": 1, "dimension": 1024, "backend": "lancedb"},
    )
    (tmp_path / "vectors.lancedb").mkdir()

    lancedb_inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert not lancedb_inspection.ok
    assert lancedb_inspection.warnings == ()
    assert any(
        "invalid vectors.json" in failure and "knowcode build" in failure
        for failure in lancedb_inspection.failures
    )

    current_path = tmp_path / "current"
    _write_vectors_json(
        current_path,
        {
            "schema_version": LanceDBVectorStore.SCHEMA_VERSION,
            "dimension": 1024,
            "backend": "lancedb",
        },
    )
    (current_path / "vectors.lancedb").mkdir()

    assert inspect_vector_index(current_path, configured_backend="lancedb").ok

    faiss_path = tmp_path / "faiss"
    _write_vectors_json(
        faiss_path,
        {"schema_version": 1, "dimension": 1024, "id_map": {}},
    )
    (faiss_path / "vectors.index").write_bytes(b"placeholder")

    faiss_inspection = inspect_vector_index(faiss_path, configured_backend="faiss")

    assert not faiss_inspection.ok
    assert faiss_inspection.warnings == ()
    assert any(
        "invalid vectors.json" in failure and "knowcode build" in failure
        for failure in faiss_inspection.failures
    )


def test_create_vector_store_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unsupported vector backend"):
        create_vector_store("weaviate", dimension=1024)


def test_inspect_accepts_a_generation_with_no_vector_plane(tmp_path: Path) -> None:
    """A derived plane leaves nothing on disk, and that is not a fault.

    The ANN index is rebuilt from the durable embeddings in ``chunks.db``, so a
    generation with neither an envelope nor a native artifact is the normal
    published shape rather than a torn publication.
    """
    inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert inspection.ok, inspection.failures


def test_inspect_still_rejects_a_half_written_vector_plane(tmp_path: Path) -> None:
    """An artifact without its envelope is a torn write, not a derived plane."""
    (tmp_path / "vectors.lancedb").mkdir()

    inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert not inspection.ok
    assert any("vectors.json" in failure for failure in inspection.failures)


def test_create_vector_store_never_persists_into_a_generation() -> None:
    """The store is a cache, so it must not write inside a published bundle."""
    store = create_vector_store("lancedb", dimension=4)

    assert store.path.startswith("memory://")  # type: ignore[attr-defined]
