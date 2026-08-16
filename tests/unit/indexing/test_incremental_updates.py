"""Incremental file updates as generation transactions (Step 15).

The reviewed defect: background and incremental indexing removed a file's
chunks *before* its replacement was parsed, chunked, and embedded, and the
chunk store and vector store were updated independently. Reproduced against the
pre-Step-15 code with no fault injection beyond a failing provider::

    after first index:        chunks=2  vectors=2
    remove_file(m.py)         # what the watch path does before re-indexing
    index_file(m.py)          -> RuntimeError('embedding provider is down')
    after failed re-index:    chunks=0  vectors=2
    dense search still returns both deleted chunk ids

One transient embedding failure therefore destroyed the file's searchable
chunks *and* left the vector store answering with ids the chunk store can no
longer resolve. Re-indexing a shrunken file was the mirror image: the removed
declaration's chunk and vector both survived forever.

Step 15 splits every file update into a preparation phase that touches no live
state and a commit phase that replaces the file's whole generation
transactionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from knowcode.data_models import CodeChunk
from knowcode.indexing.file_updates import (
    FileUpdateCommitError,
    FileUpdatePreparationError,
)
from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

DIMENSION = 3


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


@dataclass
class _Config:
    dimension: int = DIMENSION
    batch_size: int = 100


class StubProvider:
    """Deterministic embeddings with an injectable failure."""

    def __init__(self) -> None:
        self.config = _Config()
        self.fail = False
        self.batch_calls = 0
        self.embedded_texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        if self.fail:
            raise RuntimeError("embedding provider is down")
        self.embedded_texts.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]


class ShortBatchProvider(StubProvider):
    """Returns fewer embeddings than it was asked for."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts][:-1]


class WrongDimensionProvider(StubProvider):
    """Returns embeddings of the wrong width."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [[0.1, 0.2] for _ in texts]


class FlakyVectorStore:
    """Delegates to a real store, failing a chosen operation on demand."""

    def __init__(self, inner: VectorStore) -> None:
        self._inner = inner
        self.fail_on: Optional[str] = None
        self.fail_recovery = False
        self.failures = 0

    # -- fault injection -------------------------------------------------
    def _maybe_fail(self, operation: str) -> None:
        """Fail the first matching call; keep failing only if recovery must."""
        if self.fail_on != operation:
            return
        self.failures += 1
        if not self.fail_recovery:
            self.fail_on = None
        raise RuntimeError(f"vector backend failed during {operation}")

    # -- protocol --------------------------------------------------------
    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        self._maybe_fail("add")
        self._inner.add(chunk_id, embedding)

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        self._maybe_fail("upsert")
        self._inner.upsert(chunk_id, embedding)

    def remove(self, chunk_id: str) -> None:
        self._maybe_fail("remove")
        self._inner.remove(chunk_id)

    def flush(self) -> None:
        self._maybe_fail("flush")
        self._inner.flush()

    def search(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        return self._inner.search(embedding, limit=limit)

    def get_embedding(self, chunk_id: str) -> Optional[list[float]]:
        return self._inner.get_embedding(chunk_id)

    def count(self) -> int:
        return self._inner.count()

    def clear(self) -> None:
        self._inner.clear()

    def save(self, path: Path) -> None:
        self._inner.save(path)

    def load(self, path: Path) -> None:
        self._inner.load(path)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

TWO_FUNCTIONS = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
ONE_FUNCTION = "def alpha():\n    return 1\n"


@dataclass
class Harness:
    indexer: Indexer
    provider: StubProvider
    repo: SqliteChunkRepository
    vectors: FlakyVectorStore
    source: Path

    def counts(self) -> tuple[int, int]:
        return self.repo.count(), self.vectors.count()

    def chunk_ids(self) -> set[str]:
        return {chunk.id for chunk in self.repo.get_all()}

    def dense_ids(self) -> set[str]:
        return {
            chunk_id for chunk_id, _ in self.vectors.search([0.1, 0.2, 0.3], limit=50)
        }


@pytest.fixture
def harness(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Harness]:
    provider = getattr(request, "param", None) or StubProvider()
    src = tmp_path / "repo"
    src.mkdir()
    source = src / "m.py"
    source.write_text(TWO_FUNCTIONS, encoding="utf-8")

    repo = SqliteChunkRepository(tmp_path / "chunks.db", dimension=DIMENSION)
    vectors = FlakyVectorStore(VectorStore(dimension=DIMENSION))
    indexer = Indexer(provider, chunk_repo=repo, vector_store=vectors)  # type: ignore[arg-type]
    yield Harness(indexer, provider, repo, vectors, source)
    repo.close()


# ----------------------------------------------------------------------
# The reviewed defect
# ----------------------------------------------------------------------


def test_a_failed_re_index_preserves_the_previous_chunks(harness: Harness) -> None:
    """A transient embedding failure must not delete the file's generation."""
    harness.indexer.index_file(harness.source)
    before_chunks = harness.chunk_ids()
    before_counts = harness.counts()
    assert before_counts == (2, 2)

    # New content, so the durable embeddings cannot be reused and the
    # provider is on the critical path.
    harness.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    harness.provider.fail = True

    with pytest.raises(FileUpdatePreparationError):
        harness.indexer.index_file(harness.source)

    assert harness.chunk_ids() == before_chunks
    assert harness.counts() == before_counts


def test_a_failed_re_index_never_splits_chunk_and_vector_membership(
    harness: Harness,
) -> None:
    """Dense search must not answer with ids the chunk store cannot resolve."""
    harness.indexer.index_file(harness.source)
    harness.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    harness.provider.fail = True

    with pytest.raises(FileUpdatePreparationError):
        harness.indexer.index_file(harness.source)

    assert harness.dense_ids() == harness.chunk_ids()
    for chunk_id in harness.dense_ids():
        assert harness.repo.get(chunk_id) is not None


def test_replacing_a_shrunken_file_drops_its_stale_chunks_and_vectors(
    harness: Harness,
) -> None:
    """A declaration removed from the source leaves no chunk or vector behind."""
    harness.indexer.index_file(harness.source)
    assert harness.counts() == (2, 2)

    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")
    harness.indexer.index_file(harness.source)

    assert harness.counts() == (1, 1)
    assert harness.dense_ids() == harness.chunk_ids()
    assert not any("beta" in chunk_id for chunk_id in harness.chunk_ids())


# ----------------------------------------------------------------------
# Preparation touches no live state
# ----------------------------------------------------------------------


def test_preparation_does_not_mutate_live_state(harness: Harness) -> None:
    harness.indexer.index_file(harness.source)
    before = (harness.chunk_ids(), harness.counts())

    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")
    update = harness.indexer.prepare_file_update(harness.source)

    assert update.chunks, "preparation produced a complete replacement"
    assert (harness.chunk_ids(), harness.counts()) == before


def test_preparation_carries_a_validated_embedding_per_chunk(
    harness: Harness,
) -> None:
    update = harness.indexer.prepare_file_update(harness.source)

    assert len(update.chunks) == 2
    for chunk in update.chunks:
        assert chunk.embedding is not None
        assert len(chunk.embedding) == DIMENSION


def test_preparation_normalizes_the_file_identity(harness: Harness) -> None:
    alias = harness.source.parent / ".." / harness.source.parent.name / "m.py"

    update = harness.indexer.prepare_file_update(alias)

    assert update.file_path == harness.source.resolve().as_posix()


@pytest.mark.parametrize(
    "harness",
    [ShortBatchProvider(), WrongDimensionProvider()],
    indirect=True,
)
def test_preparation_rejects_an_invalid_embedding_batch(harness: Harness) -> None:
    """A short or wrong-width batch fails before anything is committed."""
    with pytest.raises(FileUpdatePreparationError):
        harness.indexer.prepare_file_update(harness.source)

    assert harness.counts() == (0, 0)


def test_a_missing_file_prepares_as_a_deletion(harness: Harness) -> None:
    harness.indexer.index_file(harness.source)
    harness.source.unlink()

    update = harness.indexer.prepare_file_update(harness.source)

    assert update.is_deletion
    assert update.chunks == ()


# ----------------------------------------------------------------------
# Commit is transactional
# ----------------------------------------------------------------------


def test_a_sqlite_commit_failure_leaves_the_previous_generation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.indexer.index_file(harness.source)
    before_chunks = harness.chunk_ids()
    before_counts = harness.counts()

    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")
    update = harness.indexer.prepare_file_update(harness.source)

    def explode(self: SqliteChunkRepository, rows: list[Any]) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(SqliteChunkRepository, "_commit_rows", explode)
    with pytest.raises(RuntimeError):
        harness.indexer.commit_file_update(update)
    monkeypatch.setattr(
        SqliteChunkRepository,
        "_commit_rows",
        SqliteChunkRepository.__dict__["_commit_rows"],
    )

    assert harness.chunk_ids() == before_chunks
    assert harness.counts() == before_counts


def test_a_vector_failure_after_the_sqlite_commit_recovers_from_durable_chunks(
    harness: Harness,
) -> None:
    """Durable embeddings (Step 08) are the recovery source, not the provider."""
    harness.indexer.index_file(harness.source)
    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")

    update = harness.indexer.prepare_file_update(harness.source)
    harness.vectors.fail_on = "upsert"
    commit = harness.indexer.commit_file_update(update)

    assert commit.recovered is True
    assert harness.counts() == (1, 1)
    assert harness.dense_ids() == harness.chunk_ids()


def test_an_unrecoverable_vector_failure_raises_a_commit_error(
    harness: Harness,
) -> None:
    harness.indexer.index_file(harness.source)
    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")

    update = harness.indexer.prepare_file_update(harness.source)
    harness.vectors.fail_on = "upsert"
    harness.vectors.fail_recovery = True

    with pytest.raises(FileUpdateCommitError) as excinfo:
        harness.indexer.commit_file_update(update)

    assert excinfo.value.recovered is False
    assert excinfo.value.file_path == harness.source.resolve().as_posix()


# ----------------------------------------------------------------------
# One replacement primitive for every mutation
# ----------------------------------------------------------------------


def test_delete_file_removes_chunks_and_vectors_together(harness: Harness) -> None:
    harness.indexer.index_file(harness.source)

    commit = harness.indexer.delete_file(harness.source)

    assert commit.is_deletion
    assert len(commit.removed_chunk_ids) == 2
    assert harness.counts() == (0, 0)
    assert harness.dense_ids() == set()


def test_remove_file_accepts_a_path_alias(harness: Harness) -> None:
    harness.indexer.index_file(harness.source)
    alias = harness.source.parent / ".." / harness.source.parent.name / "m.py"

    harness.indexer.remove_file(alias)

    assert harness.counts() == (0, 0)


def test_move_file_transfers_the_generation_to_the_new_identity(
    harness: Harness,
) -> None:
    harness.indexer.index_file(harness.source)
    moved = harness.source.parent / "renamed.py"
    harness.source.rename(moved)

    result = harness.indexer.move_file(harness.source, moved)

    assert result.committed.file_path == moved.resolve().as_posix()
    assert harness.counts() == (2, 2)
    assert all("renamed.py" in chunk_id for chunk_id in harness.chunk_ids())
    assert harness.dense_ids() == harness.chunk_ids()


def test_a_failed_move_preserves_the_source_generation(harness: Harness) -> None:
    harness.indexer.index_file(harness.source)
    before = harness.chunk_ids()
    moved = harness.source.parent / "renamed.py"
    harness.source.rename(moved)
    # Renamed *and* edited, so the move needs a fresh embedding.
    moved.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    harness.provider.fail = True

    with pytest.raises(FileUpdatePreparationError):
        harness.indexer.move_file(harness.source, moved)

    assert harness.chunk_ids() == before
    assert harness.counts() == (2, 2)


def test_repeated_edits_deletes_and_moves_keep_the_stores_in_step(
    harness: Harness,
) -> None:
    """The exit criterion: counts and searchable ids stay equal throughout."""
    other = harness.source.parent / "other.py"
    other.write_text("def gamma():\n    return 3\n", encoding="utf-8")

    harness.indexer.index_file(harness.source)
    harness.indexer.index_file(other)

    for iteration in range(3):
        harness.source.write_text(
            f"def alpha():\n    return {iteration}\n", encoding="utf-8"
        )
        harness.indexer.index_file(harness.source)
        assert harness.counts()[0] == harness.counts()[1]
        assert harness.dense_ids() == harness.chunk_ids()

        harness.source.write_text(TWO_FUNCTIONS, encoding="utf-8")
        harness.indexer.index_file(harness.source)
        assert harness.dense_ids() == harness.chunk_ids()

    renamed = other.parent / "moved.py"
    other.rename(renamed)
    harness.indexer.move_file(other, renamed)
    assert harness.dense_ids() == harness.chunk_ids()

    harness.indexer.delete_file(renamed)
    assert harness.dense_ids() == harness.chunk_ids()
    assert harness.counts() == (2, 2)


def test_unchanged_content_reuses_its_durable_embedding(harness: Harness) -> None:
    """Re-indexing must not re-embed a chunk whose content is unchanged."""
    harness.indexer.index_file(harness.source)
    harness.provider.embedded_texts.clear()

    harness.source.write_text(
        TWO_FUNCTIONS + "\n\ndef gamma():\n    return 3\n", encoding="utf-8"
    )
    update = harness.indexer.prepare_file_update(harness.source)

    assert update.reused_embeddings >= 1
    assert update.embedded_chunks == len(update.chunks) - update.reused_embeddings
    assert any("gamma" in text for text in harness.provider.embedded_texts)


def test_index_file_reports_the_committed_chunk_count(harness: Harness) -> None:
    assert harness.indexer.index_file(harness.source) == 2

    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")
    assert harness.indexer.index_file(harness.source) == 1


def test_committing_a_stale_preparation_still_replaces_the_whole_file(
    harness: Harness,
) -> None:
    """A prepared update is a complete replacement, not a delta."""
    harness.indexer.index_file(harness.source)
    harness.source.write_text(ONE_FUNCTION, encoding="utf-8")
    update = harness.indexer.prepare_file_update(harness.source)

    # Another writer adds an unrelated chunk for the same file in between.
    harness.repo.add(
        CodeChunk(
            id=f"{harness.source.resolve().as_posix()}::ghost::0",
            entity_id=f"{harness.source.resolve().as_posix()}::ghost",
            content="ghost",
            embedding=[0.9, 0.9, 0.9],
        )
    )

    commit = harness.indexer.commit_file_update(update)

    assert "ghost" not in " ".join(harness.chunk_ids())
    assert len(commit.committed_chunk_ids) == 1


# ----------------------------------------------------------------------
# A failed parse is a failed replacement, not a deletion
# ----------------------------------------------------------------------


def test_a_syntax_error_preserves_the_files_previous_generation(
    harness: Harness,
) -> None:
    """A file saved mid-edit must not fall out of the index."""
    harness.indexer.index_file(harness.source)
    before = harness.chunk_ids()

    harness.source.write_text("def alpha(:\n", encoding="utf-8")

    with pytest.raises(FileUpdatePreparationError):
        harness.indexer.index_file(harness.source)

    assert harness.chunk_ids() == before
    assert harness.counts() == (2, 2)


def test_a_bulk_build_keeps_a_broken_file_and_reports_it(harness: Harness) -> None:
    """One unparseable file must not abort a whole directory build."""
    broken = harness.source.parent / "broken.py"
    broken.write_text("def gamma(:\n", encoding="utf-8")

    harness.indexer.index_directory(harness.source.parent)

    assert harness.counts()[0] == harness.counts()[1]
    assert any(
        "broken.py" in file_path for file_path, _ in harness.indexer.failed_updates
    )
    assert not any("broken.py" in chunk_id for chunk_id in harness.chunk_ids())


def test_an_empty_file_prepares_as_a_deletion(harness: Harness) -> None:
    """Zero chunks with no parse error really is an empty file."""
    harness.indexer.index_file(harness.source)
    harness.source.write_text("", encoding="utf-8")

    commit = harness.indexer.replace_file(harness.source)

    assert commit.is_deletion
    assert harness.counts() == (0, 0)


def test_a_no_longer_indexable_file_prepares_as_a_deletion(
    harness: Harness,
) -> None:
    harness.indexer.index_file(harness.source)
    unsupported = harness.source.parent / "notes.txt"
    unsupported.write_text("plain text", encoding="utf-8")

    update = harness.indexer.prepare_file_update(unsupported)

    assert update.is_deletion


def test_a_move_onto_the_same_identity_keeps_the_generation(
    harness: Harness,
) -> None:
    """A move whose source and destination normalize equally is a no-op move."""
    harness.indexer.index_file(harness.source)
    alias = harness.source.parent / ".." / harness.source.parent.name / "m.py"

    result = harness.indexer.move_file(alias, harness.source)

    assert result.removed.is_deletion
    assert result.removed.previous_chunk_ids == ()
    assert harness.counts() == (2, 2)


def test_recovery_fails_when_a_durable_embedding_is_missing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery reads the committed chunks; without them it must give up."""
    harness.indexer.index_file(harness.source)
    harness.source.write_text("def alpha():\n    return 5\n", encoding="utf-8")
    update = harness.indexer.prepare_file_update(harness.source)

    harness.vectors.fail_on = "upsert"
    monkeypatch.setattr(SqliteChunkRepository, "get", lambda self, chunk_id: None)

    with pytest.raises(FileUpdateCommitError):
        harness.indexer.commit_file_update(update)


def test_reuse_falls_back_to_the_vector_store_for_a_row_without_an_embedding(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk row predating durable embeddings still avoids a re-embed."""
    harness.indexer.index_file(harness.source)
    stripped = {
        chunk.id: CodeChunk(
            id=chunk.id,
            entity_id=chunk.entity_id,
            content=chunk.content,
            tokens=chunk.tokens,
            embedding=None,
            metadata=chunk.metadata,
        )
        for chunk in harness.repo.get_all()
    }
    monkeypatch.setattr(
        SqliteChunkRepository, "get", lambda self, chunk_id: stripped.get(chunk_id)
    )
    harness.provider.embedded_texts.clear()

    harness.source.write_text(
        TWO_FUNCTIONS + "\n\ndef gamma():\n    return 3\n", encoding="utf-8"
    )
    update = harness.indexer.prepare_file_update(harness.source)

    assert update.reused_embeddings >= 1
    assert all("beta" not in text for text in harness.provider.embedded_texts)
