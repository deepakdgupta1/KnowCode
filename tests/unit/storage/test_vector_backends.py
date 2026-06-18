"""Tests for vector backend inspection and factory behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        {"schema_version": 1, "dimension": 1024, "backend": "lancedb"},
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
        {"schema_version": 1, "dimension": 1024, "backend": "lancedb"},
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


def test_schema_warning_uses_selected_backend(tmp_path: Path) -> None:
    _write_vectors_json(
        tmp_path,
        {"schema_version": 1, "dimension": 1024, "backend": "lancedb"},
    )
    (tmp_path / "vectors.lancedb").mkdir()

    lancedb_inspection = inspect_vector_index(tmp_path, configured_backend="lancedb")

    assert lancedb_inspection.warnings == ()

    faiss_path = tmp_path / "faiss"
    _write_vectors_json(
        faiss_path,
        {"schema_version": 1, "dimension": 1024, "id_map": {}},
    )
    (faiss_path / "vectors.index").write_bytes(b"placeholder")

    faiss_inspection = inspect_vector_index(faiss_path, configured_backend="faiss")

    assert "vector metadata was migrated in memory" in faiss_inspection.warnings


def test_create_vector_store_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unsupported vector backend"):
        create_vector_store("weaviate", dimension=1024)
