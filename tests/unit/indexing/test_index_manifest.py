"""Unit tests for index manifest persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowcode.data_models import EmbeddingConfig
from knowcode.indexing.indexer import Indexer
from knowcode.llm.embedding import EmbeddingProvider
from knowcode.utils import atomic_write
from knowcode.utils.atomic_write import TEMP_SUFFIX


class DummyEmbeddingProvider(EmbeddingProvider):
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.config.dimension for _ in texts]

    def embed_single(self, text: str) -> list[float]:
        return [0.0] * self.config.dimension


def test_indexer_writes_and_loads_manifest(tmp_path: Path) -> None:
    provider = DummyEmbeddingProvider(EmbeddingConfig(provider="openai", model_name="x", dimension=8))
    indexer = Indexer(provider)

    out_dir = tmp_path / "idx"
    indexer.save(out_dir)

    manifest_file = out_dir / "index_manifest.json"
    assert manifest_file.exists()

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == Indexer.SCHEMA_VERSION
    assert manifest["embedding"]["dimension"] == 8
    assert manifest["chunking"]["max_chunk_size"] > 0

    indexer2 = Indexer(provider)
    indexer2.load(out_dir)
    assert indexer2.manifest.get("schema_version") == Indexer.SCHEMA_VERSION
    assert indexer2.manifest.get("embedding", {}).get("dimension") == 8


def test_indexer_load_migrates_legacy_manifest(tmp_path: Path) -> None:
    """Legacy manifests without schema_version should load via migration shim."""
    provider = DummyEmbeddingProvider(EmbeddingConfig(provider="openai", model_name="x", dimension=8))
    out_dir = tmp_path / "idx"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks.json").write_text(json.dumps({"chunks": []}), encoding="utf-8")
    (out_dir / "index_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "embedding": {"dimension": 8},
                "chunking": {"max_chunk_size": 1000},
            }
        ),
        encoding="utf-8",
    )

    indexer = Indexer(provider)
    indexer.load(out_dir)
    assert indexer.manifest["schema_version"] == Indexer.SCHEMA_VERSION


def test_indexer_load_rejects_unsupported_manifest_schema(tmp_path: Path) -> None:
    """Unsupported schema versions should fail with actionable messaging."""
    provider = DummyEmbeddingProvider(EmbeddingConfig(provider="openai", model_name="x", dimension=8))
    out_dir = tmp_path / "idx"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks.json").write_text(json.dumps({"chunks": []}), encoding="utf-8")
    (out_dir / "index_manifest.json").write_text(
        json.dumps({"schema_version": 999, "embedding": {}, "chunking": {}}),
        encoding="utf-8",
    )

    indexer = Indexer(provider)
    with pytest.raises(ValueError, match="schema version"):
        indexer.load(out_dir)





def test_indexer_manifest_migrates_schema_version_one() -> None:
    """Explicit schema_version=1 should migrate to current schema."""
    migrated = Indexer._validate_and_migrate_manifest({"schema_version": 1})
    assert migrated["schema_version"] == Indexer.SCHEMA_VERSION





# --- Step 13: crash-safe manifest replacement ------------------------------


def _indexer(dimension: int = 8) -> Indexer:
    provider = DummyEmbeddingProvider(
        EmbeddingConfig(provider="openai", model_name="x", dimension=dimension)
    )
    return Indexer(provider)


def test_manifest_write_failure_preserves_the_previous_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed manifest replacement leaves the last good manifest loadable.

    Cross-artifact staging (a manifest that fails after new vectors landed)
    belongs to Step 14; Step 13 only guarantees that the manifest file itself is
    never truncated.
    """
    out_dir = tmp_path / "idx"
    indexer = _indexer()
    indexer.save(out_dir)
    previous = json.loads(
        (out_dir / "index_manifest.json").read_text(encoding="utf-8")
    )

    real_replace = atomic_write._replace

    def boom(source: Path, destination: Path) -> None:
        if Path(destination).name == "index_manifest.json":
            raise OSError("No space left on device")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write, "_replace", boom)
    indexer.manifest["last_indexed_commit"] = "deadbeef"
    with pytest.raises(OSError):
        indexer.save(out_dir)
    monkeypatch.undo()

    assert json.loads(
        (out_dir / "index_manifest.json").read_text(encoding="utf-8")
    ) == previous


def test_save_publishes_data_before_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 4 publication ordering: vectors and chunks, then the manifest last."""
    out_dir = tmp_path / "idx"
    indexer = _indexer()
    order: list[str] = []

    real_vectors_save = indexer.vector_store.save
    real_chunks_save = indexer.chunk_repo.save
    real_replace = atomic_write._replace

    def vectors_save(path: Path) -> None:
        order.append("vectors")
        real_vectors_save(path)

    def chunks_save(path: Path) -> None:
        order.append("chunks")
        real_chunks_save(path)

    def replace_spy(source: Path, destination: Path) -> None:
        if Path(destination).name == "index_manifest.json":
            order.append("manifest")
        real_replace(source, destination)

    monkeypatch.setattr(indexer.vector_store, "save", vectors_save)
    monkeypatch.setattr(indexer.chunk_repo, "save", chunks_save)
    monkeypatch.setattr(atomic_write, "_replace", replace_spy)

    indexer.save(out_dir)

    assert order == ["vectors", "chunks", "manifest"]


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    """Publication cleans up its staging files."""
    out_dir = tmp_path / "idx"
    _indexer().save(out_dir)

    leftovers = [p.name for p in out_dir.iterdir() if p.name.endswith(TEMP_SUFFIX)]
    assert leftovers == []


def test_load_rejects_a_truncated_manifest(tmp_path: Path) -> None:
    """A half-written pre-Step-13 manifest fails closed with rebuild guidance."""
    out_dir = tmp_path / "idx"
    indexer = _indexer()
    indexer.save(out_dir)
    manifest_file = out_dir / "index_manifest.json"
    text = manifest_file.read_text(encoding="utf-8")
    manifest_file.write_text(text[: len(text) // 2], encoding="utf-8")

    with pytest.raises(ValueError, match="knowcode build"):
        _indexer().load(out_dir)


def test_load_removes_orphaned_temporary_files(tmp_path: Path) -> None:
    """A crash between staging and replace leaves orphans; startup clears them."""
    out_dir = tmp_path / "idx"
    indexer = _indexer()
    indexer.save(out_dir)
    orphan = out_dir / f".index_manifest.json.pid999999.abcd{TEMP_SUFFIX}"
    orphan.write_text("{partial", encoding="utf-8")

    _indexer().load(out_dir)

    assert not orphan.exists()
    assert (out_dir / "index_manifest.json").exists()
