"""Cross-file embedding batching in the bulk index pipelines.

Before this, ``index_directory`` embedded one file per ``embed`` call and ran
those calls one after another, so the wall-clock cost of ``knowcode build`` was
N sequential network round-trips. Batching cuts across file boundaries and a
bounded pool overlaps the batches.

What must not move while that happens:

* **Parity.** ``build_generation`` asserts chunk/vector counts match exactly
  before publishing, so every committed chunk still carries its own vector.
* **Failure isolation.** A file is still committed by its own
  ``replace_file`` transaction, and a file that cannot be embedded still keeps
  its previous generation while its neighbours commit. A failed *batch* falls
  back to embedding its files one at a time so a single poisonous file cannot
  take the window down with it.
* **Progress.** ``on_progress(files_done, files_total)`` still counts committed
  files, and a callback that raises still cannot break the build.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from knowcode.data_models import CodeChunk
from knowcode.indexing.embedding_batch import BatchEmbedder, RetryPolicy
from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

DIMENSION = 3
NO_RETRY = RetryPolicy(delays=())


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


@dataclass
class _Config:
    dimension: int = DIMENSION
    batch_size: int = 100


class BatchRecordingProvider:
    """Records each batch it was handed, so cross-file batching is visible."""

    def __init__(self, batch_size: int = 100) -> None:
        self.config = _Config(batch_size=batch_size)
        self.batches: list[list[str]] = []
        self.poison: Optional[str] = None
        self.fail_everything = False
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.batches.append(list(texts))
            if self.fail_everything:
                raise RuntimeError("embedding provider is down")
            poison = self.poison
        if poison is not None and any(poison in text for text in texts):
            raise RuntimeError(f"provider rejected a batch containing {poison}")
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def call_count(self) -> int:
        return len(self.batches)


class ConcurrencyProbeProvider(BatchRecordingProvider):
    """Blocks each call until ``release_at`` of them overlap."""

    def __init__(self, release_at: int, batch_size: int = 1) -> None:
        super().__init__(batch_size=batch_size)
        self.release_at = release_at
        self.in_flight = 0
        self.peak_in_flight = 0
        self._all_in = threading.Event()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            if self.in_flight >= self.release_at:
                self._all_in.set()
        self._all_in.wait(timeout=5.0)
        with self._lock:
            self.in_flight -= 1
        return super().embed(texts)


class CountingRepository(SqliteChunkRepository):
    """A real repository that counts the per-file replace transactions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.replaced_files: list[str] = []

    def replace_file(self, file_path: str | Path, chunks: list[CodeChunk]) -> Any:
        self.replaced_files.append(str(file_path))
        return super().replace_file(file_path, chunks)


@dataclass
class ProgressLog:
    """Every ``on_progress`` call, in order."""

    calls: list[tuple[int, Optional[int]]] = field(default_factory=list)

    def __call__(self, done: int, total: Optional[int]) -> None:
        self.calls.append((done, total))


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _source(tag: str) -> str:
    """Two functions whose text identifies the file they came from."""
    return f"def alpha_{tag}():\n    return '{tag}'\n\n\ndef beta_{tag}():\n    return '{tag}'\n"


@dataclass
class Harness:
    indexer: Indexer
    provider: BatchRecordingProvider
    repo: CountingRepository
    vectors: VectorStore
    root: Path

    def counts(self) -> tuple[int, int]:
        return self.repo.count(), self.vectors.count()

    def chunk_ids(self) -> list[str]:
        return [chunk.id for chunk in self.repo.get_all()]


def _build_harness(
    tmp_path: Path,
    *,
    files: int = 6,
    provider: Optional[BatchRecordingProvider] = None,
    batch_size: Optional[int] = None,
    max_workers: int = 4,
) -> Harness:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for index in range(files):
        (root / f"mod{index}.py").write_text(_source(f"m{index}"), encoding="utf-8")

    provider = provider or BatchRecordingProvider()
    repo = CountingRepository(":memory:", dimension=DIMENSION)
    vectors = VectorStore(dimension=DIMENSION)
    indexer = Indexer(
        provider,
        chunk_repo=repo,
        vector_store=vectors,
        batch_embedder=BatchEmbedder(
            provider,
            batch_size=batch_size,
            max_workers=max_workers,
            retry=NO_RETRY,
        ),
    )
    return Harness(indexer, provider, repo, vectors, root)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return _build_harness(tmp_path)


# ----------------------------------------------------------------------
# Batching across files
# ----------------------------------------------------------------------


def test_a_directory_build_batches_chunks_across_files(harness: Harness) -> None:
    """Six files used to cost six round-trips; one batch now covers them."""
    chunks = harness.indexer.index_directory(harness.root)

    assert chunks > 0
    assert harness.provider.call_count < 6
    assert any(len(batch) > 2 for batch in harness.provider.batches)


def test_one_batch_carries_chunks_from_more_than_one_file(harness: Harness) -> None:
    """The whole point: the cut is the provider's limit, not a file boundary."""
    harness.indexer.index_directory(harness.root)

    tags_per_batch = [
        {tag for tag in ("m0", "m1", "m2") if any(tag in text for text in batch)}
        for batch in harness.provider.batches
    ]
    assert any(len(tags) > 1 for tags in tags_per_batch)


def test_batching_preserves_chunk_vector_parity(harness: Harness) -> None:
    """``build_generation`` refuses to publish unless these match exactly."""
    chunks = harness.indexer.index_directory(harness.root)

    committed, vectors = harness.counts()
    assert committed == vectors == chunks


def test_every_file_is_still_its_own_commit_transaction(harness: Harness) -> None:
    """Batching moved the embedding, not the transaction boundary."""
    harness.indexer.index_directory(harness.root)

    assert sorted(harness.repo.replaced_files) == sorted(
        set(harness.repo.replaced_files)
    )
    assert len(harness.repo.replaced_files) == 6


def test_the_provider_batch_limit_is_honoured(tmp_path: Path) -> None:
    harness = _build_harness(tmp_path, files=6, batch_size=3)

    harness.indexer.index_directory(harness.root)

    assert harness.provider.batches
    assert all(len(batch) <= 3 for batch in harness.provider.batches)


def test_embedding_batches_overlap_during_a_directory_build(tmp_path: Path) -> None:
    """A sequential loop cannot make two calls overlap."""
    provider = ConcurrencyProbeProvider(release_at=2, batch_size=1)
    harness = _build_harness(
        tmp_path, files=4, provider=provider, batch_size=1, max_workers=4
    )

    harness.indexer.index_directory(harness.root)

    assert provider.peak_in_flight >= 2
    assert harness.counts()[0] == harness.counts()[1]


# ----------------------------------------------------------------------
# Failure isolation
# ----------------------------------------------------------------------


def test_a_file_the_provider_rejects_does_not_take_its_batch_down(
    tmp_path: Path,
) -> None:
    """A failed batch retries per file, so only the bad file is lost."""
    harness = _build_harness(tmp_path, files=4)
    (harness.root / "bad.py").write_text(_source("POISON"), encoding="utf-8")
    harness.provider.poison = "POISON"

    chunks = harness.indexer.index_directory(harness.root)

    assert chunks > 0
    assert [path for path, _ in harness.indexer.failed_updates] == [
        str((harness.root / "bad.py").resolve())
    ]
    assert not any("bad.py" in chunk_id for chunk_id in harness.chunk_ids())
    assert any("mod0.py" in chunk_id for chunk_id in harness.chunk_ids())
    assert harness.counts()[0] == harness.counts()[1] == chunks


def test_a_rejected_file_keeps_its_previous_generation(tmp_path: Path) -> None:
    """The Step 15 guarantee, under batching: no file is dropped on failure."""
    harness = _build_harness(tmp_path, files=2)
    victim = harness.root / "mod0.py"

    harness.indexer.index_directory(harness.root)
    before = sorted(harness.chunk_ids())
    assert before

    victim.write_text(_source("m0_edited_POISON"), encoding="utf-8")
    harness.provider.poison = "POISON"
    harness.indexer.index_directory(harness.root)

    assert sorted(harness.chunk_ids()) == before
    assert any("mod0.py" in path for path, _ in harness.indexer.failed_updates)
    assert harness.counts()[0] == harness.counts()[1]


def test_a_total_provider_outage_leaves_every_generation_intact(
    tmp_path: Path,
) -> None:
    harness = _build_harness(tmp_path, files=3)
    harness.indexer.index_directory(harness.root)
    before = sorted(harness.chunk_ids())

    harness.provider.fail_everything = True
    harness.indexer.index_directory(harness.root)

    assert sorted(harness.chunk_ids()) == before
    assert len(harness.indexer.failed_updates) == 3
    assert harness.counts()[0] == harness.counts()[1]


def test_an_unparseable_file_is_reported_and_the_rest_still_commit(
    tmp_path: Path,
) -> None:
    harness = _build_harness(tmp_path, files=3)
    (harness.root / "broken.py").write_text("def gamma(:\n", encoding="utf-8")

    chunks = harness.indexer.index_directory(harness.root)

    assert chunks > 0
    assert any("broken.py" in path for path, _ in harness.indexer.failed_updates)
    assert harness.counts()[0] == harness.counts()[1] == chunks


def test_a_failed_batch_is_not_retried_per_file_forever(tmp_path: Path) -> None:
    """The fallback spends one attempt per file, not a fresh retry budget."""
    harness = _build_harness(tmp_path, files=3)
    harness.provider.fail_everything = True

    harness.indexer.index_directory(harness.root)

    # One failed window batch, then exactly one attempt per file.
    assert harness.provider.call_count == 1 + 3


# ----------------------------------------------------------------------
# Progress
# ----------------------------------------------------------------------


def test_progress_counts_every_file_from_zero(harness: Harness) -> None:
    progress = ProgressLog()

    harness.indexer.index_directory(harness.root, on_progress=progress)

    assert progress.calls[0] == (0, 6)
    assert progress.calls[-1] == (6, 6)
    done = [done for done, _ in progress.calls]
    assert done == sorted(done)
    assert set(done) == {0, 1, 2, 3, 4, 5, 6}


def test_progress_counts_files_that_failed(tmp_path: Path) -> None:
    """A file kept at its previous generation is still a file we are done with."""
    harness = _build_harness(tmp_path, files=3)
    harness.provider.fail_everything = True
    progress = ProgressLog()

    harness.indexer.index_directory(harness.root, on_progress=progress)

    assert progress.calls[-1] == (3, 3)


def test_a_raising_progress_callback_cannot_break_the_build(harness: Harness) -> None:
    def explode(done: int, total: Optional[int]) -> None:
        raise RuntimeError("the reporter is broken, the build is not")

    chunks = harness.indexer.index_directory(harness.root, on_progress=explode)

    assert chunks > 0
    assert harness.counts()[0] == harness.counts()[1] == chunks


def test_a_build_without_a_callback_still_works(harness: Harness) -> None:
    assert harness.indexer.index_directory(harness.root) > 0


# ----------------------------------------------------------------------
# Incremental
# ----------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_harness(tmp_path: Path) -> Harness:
    harness = _build_harness(tmp_path, files=3)
    _git(harness.root, "init")
    _git(harness.root, "config", "user.email", "test@example.com")
    _git(harness.root, "config", "user.name", "Test")
    _git(harness.root, "add", ".")
    _git(harness.root, "commit", "-m", "initial")
    return harness


def test_incremental_batches_across_changed_files(git_harness: Harness) -> None:
    git_harness.indexer.index_directory(git_harness.root)
    _git(git_harness.root, "add", ".")

    for index in range(3):
        (git_harness.root / f"mod{index}.py").write_text(
            _source(f"m{index}_v2"), encoding="utf-8"
        )
    _git(git_harness.root, "add", ".")
    _git(git_harness.root, "commit", "-m", "second")

    git_harness.provider.batches.clear()
    progress = ProgressLog()
    chunks = git_harness.indexer.index_incremental(
        git_harness.root, on_progress=progress
    )

    assert chunks > 0
    assert git_harness.provider.call_count < 3
    assert progress.calls[0][0] == 0
    assert progress.calls[-1] == (progress.calls[-1][1], progress.calls[-1][1])
    assert git_harness.counts()[0] == git_harness.counts()[1]


def test_incremental_isolates_a_rejected_file(git_harness: Harness) -> None:
    git_harness.indexer.index_directory(git_harness.root)
    before = sorted(git_harness.chunk_ids())

    (git_harness.root / "mod0.py").write_text(_source("m0_POISON"), encoding="utf-8")
    (git_harness.root / "mod1.py").write_text(_source("m1_v2"), encoding="utf-8")
    _git(git_harness.root, "add", ".")
    _git(git_harness.root, "commit", "-m", "second")
    git_harness.provider.poison = "POISON"

    git_harness.indexer.index_incremental(git_harness.root)

    assert any("mod0.py" in path for path, _ in git_harness.indexer.failed_updates)
    assert sorted(git_harness.chunk_ids()) != before  # mod1 did commit
    assert any("mod0.py" in chunk_id for chunk_id in git_harness.chunk_ids())
    assert git_harness.counts()[0] == git_harness.counts()[1]


# ----------------------------------------------------------------------
# Single-file paths are unchanged
# ----------------------------------------------------------------------


def test_indexing_one_file_still_uses_one_batch(harness: Harness) -> None:
    harness.indexer.index_file(harness.root / "mod0.py")

    assert harness.provider.call_count == 1
    assert harness.counts()[0] == harness.counts()[1]


# ----------------------------------------------------------------------
# Who owns the retry budget
# ----------------------------------------------------------------------


class RecoveringProvider(BatchRecordingProvider):
    """Fails a set number of calls, then answers normally."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining_failures = failures

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.batches.append(list(texts))
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                raise RuntimeError("embedding provider is down")
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_a_bulk_build_retries_a_transient_provider_failure(tmp_path: Path) -> None:
    """A build has no outer loop, so a blip must not cost it the generation."""
    slept: list[float] = []
    provider = RecoveringProvider(failures=2)
    harness = _build_harness(tmp_path, files=3, provider=provider)
    harness.indexer._batch_embedder = BatchEmbedder(
        provider,
        max_workers=1,
        retry=RetryPolicy(delays=(0.5, 2.0)),
        sleep=slept.append,
    )

    chunks = harness.indexer.index_directory(harness.root)

    assert chunks > 0
    assert slept == [0.5, 2.0]
    assert harness.indexer.failed_updates == []
    assert harness.counts()[0] == harness.counts()[1] == chunks


def test_one_files_transaction_does_not_retry_underneath_the_watch_worker(
    tmp_path: Path,
) -> None:
    """The worker owns the retry for a watched file (Step 16).

    Retrying inside the file transaction as well would multiply the two
    budgets and stall the watch queue for the minutes its design refuses to
    stall for, so ``replace_file`` must make exactly one attempt.
    """
    slept: list[float] = []
    provider = RecoveringProvider(failures=99)
    harness = _build_harness(tmp_path, files=1, provider=provider)
    harness.indexer._batch_embedder = BatchEmbedder(
        provider,
        max_workers=1,
        retry=RetryPolicy(delays=(0.5, 2.0)),
        sleep=slept.append,
    )

    with pytest.raises(Exception) as excinfo:
        harness.indexer.replace_file(harness.root / "mod0.py")

    assert provider.call_count == 1
    assert slept == []
    # Still classified as worth another attempt, so the worker will retry it.
    assert getattr(excinfo.value, "retryable", False) is True
