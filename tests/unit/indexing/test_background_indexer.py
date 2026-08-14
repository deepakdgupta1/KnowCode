"""Unit tests for background indexing."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest

from knowcode.indexing import background_indexer
from knowcode.indexing.background_indexer import BackgroundIndexer
from knowcode.indexing.indexer import Indexer
from knowcode.indexing.watch_queue import WatchQueueClosed
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.utils.entity_identity import normalize_file_identity

TWO_FUNCTIONS = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"

#: Every blocking wait in this module is bounded, so a regression fails the
#: test instead of hanging the suite.
TIMEOUT = 5.0


class DummyIndexer:
    """Records the generation transactions the worker commits.

    ``gate`` lets a test hold a commit open — the deterministic way to observe
    the worker mid-transaction without sleeping.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.failures: dict[str, Exception] = {}
        self.fail_times: dict[str, int] = {}
        self._lock = threading.Lock()

    def fail_on(self, path: Path, error: Exception, times: int = 10**6) -> None:
        identity = normalize_file_identity(path)
        self.failures[identity] = error
        self.fail_times[identity] = times

    def _maybe_fail(self, identity: str) -> None:
        with self._lock:
            remaining = self.fail_times.get(identity, 0)
            if remaining <= 0:
                return
            self.fail_times[identity] = remaining - 1
        raise self.failures[identity]

    def replace_file(self, path: str | Path, **kwargs: object) -> object:
        self.entered.set()
        assert self.gate.wait(timeout=TIMEOUT), "commit gate was never released"
        identity = normalize_file_identity(path)
        self._maybe_fail(identity)
        with self._lock:
            self.calls.append(identity)
        return object()

    def delete_file(self, path: str | Path) -> object:
        identity = normalize_file_identity(path)
        self._maybe_fail(identity)
        with self._lock:
            self.deleted.append(identity)
        return object()

    def move_file(self, old_path: str | Path, new_path: str | Path) -> object:
        raise AssertionError("the worker commits moves as replace + drop")

    def hold(self) -> None:
        """Make the next commit block until :meth:`release`."""
        self.entered.clear()
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()


@pytest.fixture
def worker_for():  # type: ignore[no-untyped-def]
    """Build workers that are always stopped, even when a test fails."""
    built: list[BackgroundIndexer] = []

    def build(indexer: object, **kwargs: object) -> BackgroundIndexer:
        worker = BackgroundIndexer(indexer, **kwargs)  # type: ignore[arg-type]
        built.append(worker)
        return worker

    yield build

    for worker in built:
        if isinstance(worker.indexer, DummyIndexer):
            worker.indexer.release()
        worker.stop(timeout=TIMEOUT)


def test_background_indexer_processes_queue(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Queued files should be processed by the worker thread."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)

    assert bg.drain(timeout=TIMEOUT).completed
    assert indexer.calls == [normalize_file_identity(target)]


# ----------------------------------------------------------------------
# Coalescing through the worker (Step 16)
# ----------------------------------------------------------------------


def test_a_burst_of_modify_events_commits_once(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The reviewed defect: five modify events committed five transactions."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    source = tmp_path / "m.py"
    for _ in range(5):
        bg.queue_file(source)

    bg.start()
    assert bg.drain(timeout=TIMEOUT).completed

    assert indexer.calls == [normalize_file_identity(source)]


def test_an_edit_during_a_commit_gets_its_own_transaction(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The in-flight commit read the old bytes; the new save needs a fresh read."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    source = tmp_path / "m.py"

    indexer.hold()
    bg.start()
    bg.queue_file(source)
    assert indexer.entered.wait(timeout=TIMEOUT)

    bg.queue_file(source)
    indexer.release()
    assert bg.drain(timeout=TIMEOUT).completed

    assert indexer.calls == [normalize_file_identity(source)] * 2


def test_a_create_modify_delete_burst_commits_the_delete(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    source = tmp_path / "m.py"

    bg.queue_file(source)
    bg.queue_file(source)
    bg.queue_removal(source)
    bg.start()
    assert bg.drain(timeout=TIMEOUT).completed

    assert indexer.calls == []
    assert indexer.deleted == [normalize_file_identity(source)]


def test_a_move_chain_commits_one_transaction(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """`a -> b -> c` indexes only `c`, and drops both earlier identities."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    a, b, c = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"

    bg.queue_move(a, b)
    bg.queue_move(b, c)
    bg.start()
    assert bg.drain(timeout=TIMEOUT).completed

    assert indexer.calls == [normalize_file_identity(c)]
    assert set(indexer.deleted) == {
        normalize_file_identity(a),
        normalize_file_identity(b),
    }


# ----------------------------------------------------------------------
# Lifecycle: idempotent start/stop and a bounded, honest drain
# ----------------------------------------------------------------------


def test_starting_twice_does_not_create_a_second_worker(worker_for) -> None:  # type: ignore[no-untyped-def]
    """Two consumers on one queue commit two files at once and only one joins."""
    bg = worker_for(DummyIndexer())

    assert bg.start() is True
    first = bg._thread
    assert bg.start() is False
    assert bg._thread is first

    report = bg.stop(timeout=TIMEOUT)

    assert report.completed
    assert first is not None and not first.is_alive()
    assert bg.is_running is False


def test_stop_drains_queued_work_before_returning(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The reviewed defect: stop() committed 1 of 3 queued files and returned None."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    sources = [tmp_path / f"m{n}.py" for n in range(3)]

    indexer.hold()
    bg.start()
    bg.queue_file(sources[0])
    assert indexer.entered.wait(timeout=TIMEOUT)
    for source in sources[1:]:
        bg.queue_file(source)
    indexer.release()

    report = bg.stop(timeout=TIMEOUT)

    assert report.completed
    assert report.pending == ()
    assert report.incomplete_work == ()
    assert set(indexer.calls) == {normalize_file_identity(s) for s in sources}


def test_stop_reports_the_work_it_could_not_drain(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A bounded drain must never claim durability it does not have."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    stuck, queued = tmp_path / "stuck.py", tmp_path / "queued.py"

    indexer.hold()
    bg.start()
    bg.queue_file(stuck)
    assert indexer.entered.wait(timeout=TIMEOUT)
    bg.queue_file(queued)

    report = bg.stop(timeout=0.2)

    assert report.completed is False
    assert report.in_flight is not None
    assert report.in_flight.path == normalize_file_identity(stuck)
    assert [work.path for work in report.pending] == [normalize_file_identity(queued)]
    assert [work.path for work in bg.pending()] == [normalize_file_identity(queued)]
    assert set(report.incomplete_work) == {
        normalize_file_identity(stuck),
        normalize_file_identity(queued),
    }


def test_work_queued_after_stop_is_rejected_not_dropped(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    bg = worker_for(DummyIndexer())
    bg.start()
    bg.stop(timeout=TIMEOUT)

    with pytest.raises(WatchQueueClosed):
        bg.queue_file(tmp_path / "m.py")
    with pytest.raises(WatchQueueClosed):
        bg.queue_removal(tmp_path / "m.py")
    with pytest.raises(WatchQueueClosed):
        bg.queue_move(tmp_path / "m.py", tmp_path / "n.py")


def test_stop_is_idempotent(worker_for) -> None:  # type: ignore[no-untyped-def]
    bg = worker_for(DummyIndexer())
    bg.start()

    assert bg.stop(timeout=TIMEOUT).completed
    assert bg.stop(timeout=TIMEOUT).completed
    assert bg.is_running is False


def test_stopping_a_worker_that_never_started_is_clean(worker_for) -> None:  # type: ignore[no-untyped-def]
    report = worker_for(DummyIndexer()).stop(timeout=TIMEOUT)

    assert report.completed
    assert report.pending == ()


def test_a_stopped_worker_cannot_be_restarted(worker_for) -> None:  # type: ignore[no-untyped-def]
    """Restarting would run a worker whose queue rejects every event."""
    bg = worker_for(DummyIndexer())
    bg.start()
    bg.stop(timeout=TIMEOUT)

    with pytest.raises(RuntimeError, match="create a new one"):
        bg.start()


# ----------------------------------------------------------------------
# Retry classification (Step 16)
# ----------------------------------------------------------------------


class _RecordingSleep:
    """A backoff that records instead of waiting.

    ``on_sleep`` is where a test changes the world *during* a retry backoff —
    a provider coming back up — with no wall-clock race to lose.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.on_sleep: Optional[Callable[[], None]] = None

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.on_sleep is not None:
            self.on_sleep()


def test_a_retryable_failure_is_retried_with_backoff(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("file is still being written"), times=1)
    slept = _RecordingSleep()
    bg = worker_for(indexer, retry_delays=(0.5, 2.0), sleep=slept)

    bg.start()
    bg.queue_file(source)
    report = bg.drain(timeout=TIMEOUT)

    assert report.completed
    assert indexer.calls == [normalize_file_identity(source)]
    assert slept.delays == [0.5]
    assert bg.failures() == ()


def test_retries_are_bounded_and_the_exhausted_work_is_reported(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("device is offline"))
    slept = _RecordingSleep()
    bg = worker_for(indexer, max_attempts=3, retry_delays=(0.1, 0.2), sleep=slept)

    bg.start()
    bg.queue_file(source)
    report = bg.drain(timeout=TIMEOUT)

    assert slept.delays == [0.1, 0.2]
    assert report.completed is False
    (failure,) = bg.failures()
    assert failure.path == normalize_file_identity(source)
    assert failure.operation == "index"
    assert failure.attempts == 3
    assert failure.exhausted is True
    assert "device is offline" in failure.reason


def test_an_unrecognized_failure_is_terminal_and_reported(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Fail closed: an unclassified error is surfaced, never retried blindly."""
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, ValueError("unrecognized"))
    slept = _RecordingSleep()
    bg = worker_for(indexer, sleep=slept)

    bg.start()
    bg.queue_file(source)
    bg.drain(timeout=TIMEOUT)

    assert slept.delays == []
    (failure,) = bg.failures()
    assert failure.attempts == 1
    assert failure.exhausted is False


def test_a_newer_event_replaces_a_pending_retry(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Replaying a failed read is waste once a newer save is queued."""
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("busy"), times=1)
    slept = _RecordingSleep()
    bg = worker_for(indexer, sleep=slept)

    indexer.hold()
    bg.start()
    bg.queue_file(source)
    assert indexer.entered.wait(timeout=TIMEOUT)
    bg.queue_file(source)
    indexer.release()

    assert bg.drain(timeout=TIMEOUT).completed
    assert indexer.calls == [normalize_file_identity(source)]
    assert slept.delays == []
    assert bg.failures() == ()


def test_max_attempts_of_one_disables_retries(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("busy"), times=1)
    bg = worker_for(indexer, max_attempts=1)

    bg.start()
    bg.queue_file(source)
    bg.drain(timeout=TIMEOUT)

    assert indexer.calls == []
    assert bg.failures()[0].attempts == 1


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        BackgroundIndexer(DummyIndexer(), max_attempts=0)  # type: ignore[arg-type]


def test_no_configured_backoff_retries_immediately(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("busy"), times=1)
    slept = _RecordingSleep()
    bg = worker_for(indexer, retry_delays=(), sleep=slept)

    bg.start()
    bg.queue_file(source)

    assert bg.drain(timeout=TIMEOUT).completed
    assert slept.delays == []
    assert indexer.calls == [normalize_file_identity(source)]


def test_a_drain_does_not_spend_its_budget_backing_off(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Shutdown is bounded even when the work in flight is failing and retrying."""
    indexer = DummyIndexer()
    source = tmp_path / "m.py"
    indexer.fail_on(source, OSError("device is offline"))
    slept = _RecordingSleep()
    bg = worker_for(indexer, max_attempts=3, retry_delays=(30.0,), sleep=slept)

    def release_once_shutdown_starts() -> None:
        """Let the commit fail only after stop() has closed the queue."""
        deadline = time.monotonic() + TIMEOUT
        while not bg._queue.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        indexer.release()

    indexer.hold()
    bg.start()
    bg.queue_file(source)
    assert indexer.entered.wait(timeout=TIMEOUT)
    releaser = threading.Thread(target=release_once_shutdown_starts)
    releaser.start()
    try:
        report = bg.stop(timeout=TIMEOUT)
    finally:
        releaser.join(timeout=TIMEOUT)

    assert slept.delays == []
    assert report.completed is False
    assert bg.failures()[0].attempts == 3


def test_the_worker_survives_a_failure_in_its_own_failure_handling(  # type: ignore[no-untyped-def]
    worker_for, tmp_path: Path
) -> None:
    """Dying here would leave producers queueing work nothing consumes."""
    indexer = DummyIndexer()
    broken, later = tmp_path / "broken.py", tmp_path / "later.py"
    indexer.fail_on(broken, OSError("busy"))

    def explode(delay: float) -> None:
        raise RuntimeError("the backoff clock is broken")

    bg = worker_for(indexer, sleep=explode)
    bg.start()
    bg.queue_file(broken)
    bg.drain(timeout=TIMEOUT)

    bg.queue_file(later)

    assert bg.drain(timeout=TIMEOUT).pending == ()
    assert indexer.calls == [normalize_file_identity(later)]
    assert bg.is_running


def test_an_idle_worker_keeps_consuming(worker_for, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The poll timeout is a liveness backstop, not an exit condition.

    The sleep only lets an idle poll cycle elapse; the assertion is on the
    commit that follows it.
    """
    monkeypatch.setattr(background_indexer, "_TAKE_TIMEOUT", 0.01)
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    source = tmp_path / "m.py"

    bg.start()
    time.sleep(0.1)  # ten poll cycles at the patched timeout
    assert bg.is_running

    bg.queue_file(source)
    assert bg.drain(timeout=TIMEOUT).completed
    assert indexer.calls == [normalize_file_identity(source)]


# ----------------------------------------------------------------------
# The watch path commits generation transactions (Step 15)
# ----------------------------------------------------------------------


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
    slept: _RecordingSleep

    def counts(self) -> tuple[int, int]:
        return self.repo.count(), self.vectors.count()

    def chunk_ids(self) -> set[str]:
        return {chunk.id for chunk in self.repo.get_all()}

    def dense_ids(self) -> set[str]:
        return {cid for cid, _ in self.vectors.search([0.1, 0.2, 0.3], limit=50)}

    def drain(self) -> None:
        """Block until every queued command has been processed."""
        assert self.worker.drain(timeout=TIMEOUT).pending == ()


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
    slept = _RecordingSleep()
    worker = BackgroundIndexer(indexer, retry_delays=(0.01, 0.02), sleep=slept)
    worker.start()
    try:
        yield _Watched(worker, indexer, provider, repo, vectors, source, slept)
    finally:
        worker.stop(timeout=TIMEOUT)
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


def test_a_provider_outage_is_retried_and_the_file_lands(watched) -> None:  # type: ignore[no-untyped-def]
    """A transient outage must not need a second save to be indexed (Step 16)."""
    watched.indexer.index_file(watched.source)
    watched.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.provider.fail = True

    # The provider recovers while the worker is backing off.
    watched.slept.on_sleep = lambda: setattr(watched.provider, "fail", False)
    watched.worker.queue_file(watched.source)
    watched.drain()

    assert watched.slept.delays == [0.01]
    assert watched.counts() == (1, 1)
    assert watched.dense_ids() == watched.chunk_ids()
    assert watched.worker.failures() == ()


def test_retry_exhaustion_keeps_the_last_good_generation(watched) -> None:  # type: ignore[no-untyped-def]
    watched.indexer.index_file(watched.source)
    before = watched.chunk_ids()
    watched.source.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.provider.fail = True

    watched.worker.queue_file(watched.source)
    watched.drain()

    assert watched.chunk_ids() == before
    assert watched.counts() == (2, 2)
    (failure,) = watched.worker.failures()
    assert failure.attempts == 3
    assert "embedding provider is down" in failure.reason


def test_a_file_saved_mid_edit_is_not_retried_and_keeps_its_generation(watched) -> None:  # type: ignore[no-untyped-def]
    """Terminal: retrying re-reads the same broken bytes. The next save is the fix."""
    watched.indexer.index_file(watched.source)
    before = watched.chunk_ids()
    watched.source.write_text("def alpha(:\n", encoding="utf-8")

    watched.worker.queue_file(watched.source)
    watched.drain()

    assert watched.chunk_ids() == before
    assert watched.slept.delays == []
    (failure,) = watched.worker.failures()
    assert failure.attempts == 1
    assert failure.exhausted is False


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


def test_a_failed_move_leaves_the_source_searchable(watched) -> None:  # type: ignore[no-untyped-def]
    """Never a file that exists under neither identity."""
    watched.indexer.index_file(watched.source)
    before = watched.chunk_ids()
    moved = watched.source.parent / "renamed.py"
    watched.source.rename(moved)
    # Renamed *and* edited, so the destination needs the provider: identical
    # content would reuse its durable embeddings and never call it.
    moved.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.provider.fail = True

    watched.worker.queue_move(watched.source, moved)
    watched.drain()

    assert watched.chunk_ids() == before
    assert watched.counts() == (2, 2)
    assert watched.worker.failures()[0].path == normalize_file_identity(moved)


def test_a_failed_move_reports_the_source_it_never_dropped(watched) -> None:  # type: ignore[no-untyped-def]
    """The stale identity is the source, so naming only the destination hides it."""
    watched.indexer.index_file(watched.source)
    moved = watched.source.parent / "renamed.py"
    watched.source.rename(moved)
    moved.write_text("def alpha():\n    return 99\n", encoding="utf-8")
    watched.provider.fail = True

    watched.worker.queue_move(watched.source, moved)
    watched.drain()
    report = watched.worker.drain(timeout=TIMEOUT)

    (failure,) = watched.worker.failures()
    assert failure.dropped_paths == (normalize_file_identity(watched.source),)
    assert normalize_file_identity(watched.source) in report.incomplete_work


def test_a_move_chain_leaves_only_the_final_identity_indexed(watched) -> None:  # type: ignore[no-untyped-def]
    watched.indexer.index_file(watched.source)
    first = watched.source.parent / "first.py"
    second = watched.source.parent / "second.py"
    watched.source.rename(first)

    watched.worker.queue_move(watched.source, first)
    first.rename(second)
    watched.worker.queue_move(first, second)
    watched.drain()

    assert watched.counts() == (2, 2)
    assert all("second.py" in chunk_id for chunk_id in watched.chunk_ids())
    assert watched.dense_ids() == watched.chunk_ids()


def test_stop_commits_the_backlog_a_watcher_left_behind(watched) -> None:  # type: ignore[no-untyped-def]
    """Shutdown durability, asserted on the stores rather than on a log line."""
    other: Optional[Path] = watched.source.parent / "other.py"
    assert other is not None
    other.write_text("def gamma():\n    return 3\n", encoding="utf-8")

    watched.worker.queue_file(watched.source)
    watched.worker.queue_file(other)
    report = watched.worker.stop(timeout=TIMEOUT)

    assert report.completed
    assert watched.counts() == (3, 3)


# ----------------------------------------------------------------------
# Publication cadence (Step 18b)
# ----------------------------------------------------------------------


class PublishingIndexer(DummyIndexer):
    """A writer that stages commits and publishes them, like the real one.

    :class:`~knowcode.service_watch.ServiceWatchWriter` commits into a staging
    generation no reader can see, so the worker — the only component that knows
    when the queue has gone idle — is what turns a burst of edits into one
    published generation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.staged: list[str] = []
        self.publications: list[tuple[str, ...]] = []
        self.publish_error: Optional[Exception] = None

    def replace_file(self, path: str | Path, **kwargs: object) -> object:
        result = super().replace_file(path, **kwargs)
        with self._lock:
            self.staged.append(normalize_file_identity(path))
        return result

    def delete_file(self, path: str | Path) -> object:
        result = super().delete_file(path)
        with self._lock:
            self.staged.append(normalize_file_identity(path))
        return result

    def publish_pending(self) -> Optional[str]:
        with self._lock:
            if not self.staged:
                return None
            if self.publish_error is not None:
                raise self.publish_error
            self.publications.append(tuple(self.staged))
            self.staged.clear()
            return f"generation-{len(self.publications)}"

    def pending_paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self.staged)


def test_the_worker_publishes_when_the_queue_goes_idle(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A watched edit is only visible once its generation is published."""
    indexer = PublishingIndexer()
    bg = worker_for(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)

    assert bg.drain(timeout=TIMEOUT).completed
    assert indexer.publications == [(normalize_file_identity(target),)]


def test_a_burst_publishes_once_when_it_drains(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The cadence is per drain, not per commit: one burst, one publication."""
    indexer = PublishingIndexer()
    bg = worker_for(indexer)
    bg.start()

    indexer.hold()
    paths = []
    for index in range(3):
        target = tmp_path / f"file{index}.py"
        target.write_text("print('hi')", encoding="utf-8")
        paths.append(normalize_file_identity(target))
        bg.queue_file(target)
    assert indexer.entered.wait(timeout=TIMEOUT)
    indexer.release()

    assert bg.drain(timeout=TIMEOUT).completed
    assert indexer.publications == [tuple(paths)]


def test_stop_publishes_the_backlog_it_drained(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Shutdown must not leave a committed edit staged and unpublished."""
    indexer = PublishingIndexer()
    bg = worker_for(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)
    report = bg.stop(timeout=TIMEOUT)

    assert report.completed
    assert report.unpublished == ()
    assert indexer.publications == [(normalize_file_identity(target),)]


def test_work_that_could_not_be_published_is_reported(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A commit still in staging is stale, so it must never look durable."""
    indexer = PublishingIndexer()
    indexer.publish_error = RuntimeError("the pointer moved")
    bg = worker_for(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)
    report = bg.stop(timeout=TIMEOUT)

    identity = normalize_file_identity(target)
    assert indexer.publications == []
    assert report.unpublished == (identity,)
    assert identity in report.incomplete_work
    assert not report.completed


def test_a_writer_that_does_not_publish_is_driven_unchanged(worker_for, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A plain ``Indexer`` writes in place and has nothing to publish."""
    indexer = DummyIndexer()
    bg = worker_for(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)
    report = bg.stop(timeout=TIMEOUT)

    assert report.completed
    assert report.unpublished == ()
    assert indexer.calls == [normalize_file_identity(target)]
