"""Unit tests for index manifest persistence."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert manifest["embedding"]["dimension"] == 8
    assert manifest["chunking"]["max_chunk_size"] > 0

    indexer2 = Indexer(provider)
    indexer2.load(out_dir)
    assert indexer2.manifest.get("embedding", {}).get("dimension") == 8
