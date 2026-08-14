"""Unit tests for background indexing."""

import time
from pathlib import Path

from knowcode.indexing.background_indexer import BackgroundIndexer


class DummyIndexer:
    """Records the generation transactions the worker commits."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.deleted: list[Path] = []
        self.moved: list[tuple[Path, Path]] = []

    def replace_file(self, path: Path) -> object:
        self.calls.append(path)
        return object()

    def delete_file(self, path: Path) -> object:
        self.deleted.append(path)
        return object()

    def move_file(self, old_path: Path, new_path: Path) -> object:
        self.moved.append((old_path, new_path))
        return object()


def test_background_indexer_processes_queue(tmp_path: Path) -> None:
    """Queued files should be processed by the worker thread."""
    indexer = DummyIndexer()
    bg = BackgroundIndexer(indexer)  # type: ignore
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)

    for _ in range(20):
        if indexer.calls:
            break
        time.sleep(0.05)

    bg.stop()

    assert indexer.calls == [target]


# ----------------------------------------------------------------------
# The watch path commits generation transactions (Step 15)
# ----------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402

from knowcode.indexing.indexer import Indexer  # noqa: E402
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository  # noqa: E402
from knowcode.storage.vector_store import VectorStore  # noqa: E402

TWO_FUNCTIONS = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


@dataclass
class _Config:
    dimension: int = 3
    batch_size: int = 100


class _Provider:
    def __init__(self) -> None:
        self.config = _Config()
        self.fail = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding provider is down")
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]


@dataclass
class _Watched:
    worker: BackgroundIndexer
    indexer: Indexer
    provider: _Provider
    repo: SqliteChunkRepository
    vectors: VectorStore
    source: Path

    def counts(self) -> tuple[int, int]:
        return self.repo.count(), self.vectors.count()

    def chunk_ids(self) -> set[str]:
        return {chunk.id for chunk in self.repo.get_all()}

    def dense_ids(self) -> set[str]:
        return {cid for cid, _ in self.vectors.search([0.1, 0.2, 0.3], limit=50)}

    def drain(self) -> None:
        """Block until every queued command has been processed."""
        self.worker._queue.join()


@pytest.fixture
def watched(tmp_path: Path):  # type: ignore[no-untyped-def]
    src = tmp_path / "repo"
    src.mkdir()
    source = src / "m.py"
    source.write_text(TWO_FUNCTIONS, encoding="utf-8")

    provider = _Provider()
    repo = SqliteChunkRepository(tmp_path / "chunks.db", dimension=3)
    vectors = VectorStore(dimension=3)
    indexer = Indexer(provider, chunk_repo=repo, vector_store=vectors)
    worker = BackgroundIndexer(indexer)
    worker.start()
    try:
        yield _Watched(worker, indexer, provider, repo, vectors, source)
    finally:
        worker.stop()
        repo.close()


def test_a_failed_watched_re_index_preserves_the_previous_generation(watched) -> None:  # type: ignore[no-untyped-def]
    """The reviewed defect: the worker deleted before the replacement existed.

    Pre-Step-15 this left ``chunks=0 vectors=2`` and dense search still
    answering with both deleted chunk ids.
    """
    watched.indexer.index_file(watched.source)
    before = watched.chunk_ids()
    assert watched.counts() == (2, 2)

    watched.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.provider.fail = True
    watched.worker.queue_file(watched.source)
    watched.drain()

    assert watched.chunk_ids() == before
    assert watched.counts() == (2, 2)
    assert watched.dense_ids() == watched.chunk_ids()


def test_a_watched_re_index_replaces_the_whole_file(watched) -> None:  # type: ignore[no-untyped-def]
    watched.indexer.index_file(watched.source)

    watched.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.worker.queue_file(watched.source)
    watched.drain()

    assert watched.counts() == (1, 1)
    assert watched.dense_ids() == watched.chunk_ids()


def test_a_watched_removal_clears_chunks_and_vectors_together(watched) -> None:  # type: ignore[no-untyped-def]
    watched.indexer.index_file(watched.source)

    watched.source.unlink()
    watched.worker.queue_removal(watched.source)
    watched.drain()

    assert watched.counts() == (0, 0)


def test_a_watched_move_keeps_the_stores_in_step(watched) -> None:  # type: ignore[no-untyped-def]
    watched.indexer.index_file(watched.source)
    moved = watched.source.parent / "renamed.py"
    watched.source.rename(moved)

    watched.worker.queue_move(watched.source, moved)
    watched.drain()

    assert watched.counts() == (2, 2)
    assert watched.dense_ids() == watched.chunk_ids()
    assert all("renamed.py" in chunk_id for chunk_id in watched.chunk_ids())
