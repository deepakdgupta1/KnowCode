"""The vector index is derived, so a generation need not carry one.

ADR 0003 makes the fp32 BLOB in ``chunks.db`` the durable record, and the
incremental update path already rebuilds affected vectors from those rows. That
makes the on-disk ANN index a cache: reconstructible from bytes already
published, and worth 28% of a generation's size while it is published anyway.

These tests pin the two properties that let it stop being published. A full
corpus rebuilds from the durable rows alone, and a generation loaded without a
native vector artifact answers a semantic query exactly as one loaded with it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowcode.data_models import CodeChunk
from knowcode.indexing.generations import NATIVE_VECTOR_ARTIFACTS
from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

DIMENSION = 8


@dataclass
class _Config:
    dimension: int = DIMENSION
    batch_size: int = 100


class HashingProvider:
    """Deterministic embeddings, so a rebuild is comparable across processes."""

    def __init__(self) -> None:
        self.config = _Config()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_single(text) for text in texts]

    def embed_single(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [(byte / 127.5) - 1.0 for byte in digest[:DIMENSION]]


def _chunks(count: int) -> list[CodeChunk]:
    provider = HashingProvider()
    return [
        CodeChunk(
            id=f"f.py::e{i}::0",
            entity_id=f"f.py::e{i}",
            content=f"def fn{i}(): return {i}",
            embedding=provider.embed_single(f"def fn{i}(): return {i}"),
        )
        for i in range(count)
    ]


def _indexer(directory: Path) -> Indexer:
    """An indexer whose chunk store lives in ``directory``, as in production.

    ``SqliteChunkRepository.save`` is a no-op because SQLite persists as it
    writes, so a generation's rows exist only if the repository was opened
    inside that generation's directory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return Indexer(
        HashingProvider(),
        chunk_repo=SqliteChunkRepository(directory / "chunks.db", dimension=DIMENSION),
        vector_store=VectorStore(dimension=DIMENSION),
    )


def _delete_native_vector_artifacts(directory: Path) -> list[str]:
    removed = []
    for name in NATIVE_VECTOR_ARTIFACTS:
        artifact = directory / name
        if artifact.exists():
            artifact.unlink()
            removed.append(name)
    return removed


class TestRebuildVectorPlane:
    """``rebuild_vector_plane`` reconstructs the whole plane from chunk rows."""

    def test_places_every_durable_embedding(self, tmp_path: Path) -> None:
        indexer = _indexer(tmp_path / "index")
        chunks = _chunks(20)
        indexer.chunk_repo.add_batch(chunks)

        placed = indexer.rebuild_vector_plane()

        assert placed == 20
        assert indexer.vector_store.count() == 20
        for chunk in chunks:
            stored = indexer.vector_store.get_embedding(chunk.id)
            assert stored == pytest.approx(chunk.embedding)
        indexer.chunk_repo.close()

    def test_ignores_chunks_with_no_durable_vector(self, tmp_path: Path) -> None:
        indexer = _indexer(tmp_path / "index")
        indexer.chunk_repo.add_batch(_chunks(3))
        indexer.chunk_repo.add(
            CodeChunk(id="bare::0", entity_id="bare", content="x", embedding=None)
        )

        assert indexer.rebuild_vector_plane() == 3
        assert indexer.vector_store.get_embedding("bare::0") is None
        indexer.chunk_repo.close()

    def test_is_idempotent(self, tmp_path: Path) -> None:
        """A rebuild converges: running it twice is running it once."""
        indexer = _indexer(tmp_path / "index")
        indexer.chunk_repo.add_batch(_chunks(12))
        probe = _chunks(1)[0].embedding
        assert probe is not None

        first_count = indexer.rebuild_vector_plane()
        first_hits = indexer.vector_store.search(probe, limit=5)
        second_count = indexer.rebuild_vector_plane()
        second_hits = indexer.vector_store.search(probe, limit=5)

        assert (second_count, second_hits) == (first_count, first_hits)
        assert indexer.vector_store.count() == 12
        indexer.chunk_repo.close()

    def test_drops_vectors_whose_chunks_are_gone(self, tmp_path: Path) -> None:
        """The rebuilt plane describes the current corpus, not a stale one."""
        indexer = _indexer(tmp_path / "index")
        indexer.chunk_repo.add_batch(_chunks(5))
        indexer.rebuild_vector_plane()
        indexer.chunk_repo.clear()
        indexer.chunk_repo.add_batch(_chunks(2))

        assert indexer.rebuild_vector_plane() == 2
        assert indexer.vector_store.count() == 2
        indexer.chunk_repo.close()


class TestLoadWithoutAVectorArtifact:
    """A generation carrying no ANN index still answers semantic queries."""

    def test_search_results_match_a_generation_that_kept_its_index(
        self, tmp_path: Path
    ) -> None:
        """This is the acceptance test for dropping the published index.

        One arm loads the ANN index the old publication shape shipped. The
        other rebuilds it from the durable rows. Their answers must agree, or
        the saving costs recall.
        """
        source = tmp_path / "published"
        writer = _indexer(source)
        writer.chunk_repo.add_batch(_chunks(40))
        writer.rebuild_vector_plane()
        writer.save(source)
        # The old publication shape, written by hand: save() no longer emits it.
        writer.vector_store.save(source / "vectors")
        writer.chunk_repo.close()

        probe = HashingProvider().embed_single("def fn7(): return 7")

        with_index = _indexer(source)
        with_index.load(source)
        expected = with_index.vector_store.search(probe, limit=10)
        with_index.chunk_repo.close()

        assert _delete_native_vector_artifacts(source), "nothing to delete"

        without_index = _indexer(source)
        without_index.load(source)
        actual = without_index.vector_store.search(probe, limit=10)
        without_index.chunk_repo.close()

        assert len(expected) == 10
        assert actual == expected

    def test_rebuilt_plane_covers_every_chunk(self, tmp_path: Path) -> None:
        source = tmp_path / "published"
        writer = _indexer(source)
        writer.chunk_repo.add_batch(_chunks(31))
        writer.rebuild_vector_plane()
        writer.save(source)
        writer.chunk_repo.close()
        _delete_native_vector_artifacts(source)

        reader = _indexer(source)
        reader.load(source)

        assert reader.vector_store.count() == reader.chunk_repo.count() == 31
        reader.chunk_repo.close()

    def test_persisted_index_is_used_when_present(self, tmp_path: Path) -> None:
        """Keeping the artifact must not silently trigger a rebuild.

        The persisted plane is authoritative when it exists. A rebuild would
        produce the same vectors here, so the assertion is on the path taken,
        not on the result.
        """
        source = tmp_path / "published"
        writer = _indexer(source)
        writer.chunk_repo.add_batch(_chunks(6))
        writer.rebuild_vector_plane()
        writer.save(source)
        writer.vector_store.save(source / "vectors")
        writer.chunk_repo.close()

        reader = _indexer(source)
        reader.rebuild_vector_plane = _forbidden  # type: ignore[method-assign]
        reader.load(source)

        assert reader.vector_store.count() == 6
        reader.chunk_repo.close()


def _forbidden() -> int:
    raise AssertionError("rebuild ran against a generation that kept its index")


def test_save_writes_no_vector_artifact(tmp_path: Path) -> None:
    """The saving is exactly this: nothing vector-shaped enters the bundle."""
    source = tmp_path / "published"
    indexer = _indexer(source)
    indexer.chunk_repo.add_batch(_chunks(5))
    indexer.rebuild_vector_plane()

    indexer.save(source)
    indexer.chunk_repo.close()

    written = {entry.name for entry in source.iterdir()}
    assert written.isdisjoint(set(NATIVE_VECTOR_ARTIFACTS))
    assert "vectors.json" not in written
