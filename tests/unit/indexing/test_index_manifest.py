"""Unit tests for index manifest persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowcode.data_models import EmbeddingConfig
from knowcode.indexing.indexer import Indexer
from knowcode.llm.embedding import EmbeddingProvider


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



