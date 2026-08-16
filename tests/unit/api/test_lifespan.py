"""API lifespan ownership and teardown contract (Step 17).

The reviewed defect, reproduced against production code through its own API:
``create_app(watch=True)`` started a :class:`BackgroundIndexer` and a
:class:`FileMonitor` and nothing ever stopped them. Running a full ASGI
lifespan (startup *and* shutdown) left the worker thread running, the observer
alive, the SQLite chunk repository open, and the global service installed. Work
queued at shutdown was never committed and nothing reported the loss::

    pending at shutdown:            ['/tmp/.../n.py']
    worker still running:           True
    still pending, uncommitted:     ['/tmp/.../n.py']
    committed chunk files:          []

Step 17 gives the server one owner with one documented close order, bounded in
time, idempotent, and honest about anything it could not finish.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from knowcode.api.lifecycle import (
    SHUTDOWN_ORDER,
    ServerResources,
    ShutdownReport,
)
from knowcode.indexing.watch_queue import WatchQueueClosed
from knowcode.utils.entity_identity import normalize_file_identity

#: Every blocking wait here is bounded, so a regression fails instead of hanging.
TIMEOUT = 5.0


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


class FakeVectorStore:
    """Records whether it was flushed, and when."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.flushed = 0
        self.flush_error: Optional[Exception] = None

    def flush(self) -> None:
        self._log.append("vector.flush")
        self.flushed += 1
        if self.flush_error is not None:
            raise self.flush_error


class FakeChunkRepo:
    """Records whether it was closed, and when."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.closed = 0
        self.close_error: Optional[Exception] = None

    def close(self) -> None:
        self._log.append("repo.close")
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeIndexer:
    """A commit surface with a gate, so a test can hold shutdown mid-transaction."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.vector_store = FakeVectorStore(log)
        self.chunk_repo = FakeChunkRepo(log)
        self.committed: list[str] = []
        self.deleted: list[str] = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self._lock = threading.Lock()

    def replace_file(self, path: str | Path, **_kwargs: Any) -> object:
        self.entered.set()
        assert self.gate.wait(timeout=TIMEOUT), "commit gate was never released"
        with self._lock:
            self.committed.append(normalize_file_identity(path))
        return object()

    def delete_file(self, path: str | Path) -> object:
        with self._lock:
            self.deleted.append(normalize_file_identity(path))
        return object()

    def hold(self) -> None:
        self.entered.clear()
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()


class FakeService:
    """The narrow surface the server owner needs from the service."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.indexer = FakeIndexer(log)
        self.flush_error: Optional[Exception] = None
        self.close_error: Optional[Exception] = None
        self.flushed = 0
        self.closed = 0

    def watch_writer(self) -> FakeIndexer:
        # Step 18: the owner asks for a *writer*, not a bound indexer, so a
        # reload can move the worker onto the generation it publishes. The
        # fake hands out one object because it has only one generation.
        return self.indexer

    def flush(self) -> None:
        self._log.append("service.flush")
        self.flushed += 1
        if self.flush_error is not None:
            raise self.flush_error
        self.indexer.vector_store.flush()

    def close(self) -> None:
        self._log.append("service.close")
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error
        self.indexer.chunk_repo.close()


class FakeMonitor:
    """Stands in for the watchdog observer."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.started = 0
        self.stopped = 0
        self.start_error: Optional[Exception] = None

    def start(self) -> bool:
        if self.start_error is not None:
            raise self.start_error
        self._log.append("monitor.start")
        self.started += 1
        return True

    def stop(self) -> None:
        self._log.append("monitor.stop")
        self.stopped += 1


@pytest.fixture
def call_log() -> list[str]:
    """Ordered record of every lifecycle call, for close-order assertions."""
    return []


@pytest.fixture
def resources_for(call_log, tmp_path):  # type: ignore[no-untyped-def]
    """Build owners that are always shut down, even when a test fails."""
    built: list[ServerResources] = []

    def build(
        service: Optional[FakeService] = None,
        *,
        watch: bool = True,
        monitor: Optional[FakeMonitor] = None,
        **kwargs: Any,
    ) -> tuple[ServerResources, FakeService]:
        service = service or FakeService(call_log)
        resources = ServerResources(
            service,
            watch=watch,
            watch_root=tmp_path,
            monitor_factory=(lambda _root, _worker: monitor or FakeMonitor(call_log)),
            **kwargs,
        )
        built.append(resources)
        return resources, service

    yield build

    for resources in built:
        resources.shutdown(timeout=TIMEOUT)


# ----------------------------------------------------------------------
# The reviewed defect: nothing was ever stopped
# ----------------------------------------------------------------------


def test_shutdown_stops_the_worker_and_the_observer(resources_for) -> None:  # type: ignore[no-untyped-def]
    """Both watch resources must be stopped, not left running past the app."""
    resources, _service = resources_for()
    resources.startup()
    assert resources.worker is not None and resources.worker.is_running

    report = resources.shutdown(timeout=TIMEOUT)

    assert report.completed
    assert not resources.worker.is_running
    assert resources.monitor is None


def test_shutdown_commits_work_queued_before_it(resources_for) -> None:  # type: ignore[no-untyped-def]
    """Queued work is drained, not discarded.

    Before Step 17 the daemon worker simply died with the process: two edits
    queued at shutdown produced zero committed chunks and no report.
    """
    resources, service = resources_for()
    resources.startup()
    worker = resources.worker
    assert worker is not None

    service.indexer.hold()
    worker.queue_file("/repo/m.py")
    assert service.indexer.entered.wait(TIMEOUT), "the worker never began a commit"
    worker.queue_file("/repo/n.py")
    service.indexer.release()

    report = resources.shutdown(timeout=TIMEOUT)

    assert report.completed
    assert report.incomplete_work == ()
    assert service.indexer.committed == [
        normalize_file_identity("/repo/m.py"),
        normalize_file_identity("/repo/n.py"),
    ]


def test_shutdown_rejects_new_events_before_draining(resources_for) -> None:  # type: ignore[no-untyped-def]
    """The observer stops first, so the drain works against a closed inbox."""
    resources, _service = resources_for()
    resources.startup()
    worker = resources.worker
    assert worker is not None

    resources.shutdown(timeout=TIMEOUT)

    with pytest.raises(WatchQueueClosed):
        worker.queue_file("/repo/late.py")


# ----------------------------------------------------------------------
# Close order and ownership
# ----------------------------------------------------------------------


def test_shutdown_follows_the_documented_close_order(resources_for, call_log) -> None:  # type: ignore[no-untyped-def]
    """Stop events, drain, flush, close stores — in that order, every time."""
    resources, _service = resources_for()
    resources.startup()
    call_log.clear()

    report = resources.shutdown(timeout=TIMEOUT)

    assert call_log == [
        "monitor.stop",
        "service.flush",
        "vector.flush",
        "service.close",
        "repo.close",
    ]
    assert tuple(stage.name for stage in report.stages) == SHUTDOWN_ORDER


def test_buffered_vectors_are_flushed_before_stores_close(
    resources_for, call_log
) -> None:  # type: ignore[no-untyped-def]
    """A buffered vector write must reach the table before its store closes."""
    resources, service = resources_for()
    resources.startup()

    resources.shutdown(timeout=TIMEOUT)

    assert service.indexer.vector_store.flushed == 1
    assert service.indexer.chunk_repo.closed == 1
    assert call_log.index("vector.flush") < call_log.index("repo.close")


def test_shutdown_closes_stores_even_without_watch(resources_for, call_log) -> None:  # type: ignore[no-untyped-def]
    """Ownership does not depend on watch mode: the app opened them, it closes them."""
    resources, service = resources_for(watch=False)
    resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert report.completed
    assert service.closed == 1
    assert resources.worker is None


# ----------------------------------------------------------------------
# Failure paths: never claim durability that was not achieved
# ----------------------------------------------------------------------


def test_a_flush_failure_still_closes_the_stores(resources_for) -> None:  # type: ignore[no-untyped-def]
    """Losing buffered data must not also leak the connections."""
    resources, service = resources_for()
    service.flush_error = RuntimeError("vector table is gone")
    resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert not report.completed
    assert service.closed == 1
    flush_stage = report.stage("flush")
    assert flush_stage is not None and not flush_stage.ok
    assert "vector table is gone" in (flush_stage.detail or "")


def test_a_close_failure_is_reported_not_swallowed(resources_for) -> None:  # type: ignore[no-untyped-def]
    """A store that will not close is a diagnostic, not a silent success."""
    resources, service = resources_for()
    service.close_error = RuntimeError("connection still in use")
    resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert not report.completed
    stage = report.stage("stores")
    assert stage is not None and not stage.ok
    assert "connection still in use" in (stage.detail or "")


def test_work_that_could_not_commit_is_named(resources_for) -> None:  # type: ignore[no-untyped-def]
    """A drain that runs out of time reports the paths that are not indexed."""
    resources, service = resources_for()
    resources.startup()
    worker = resources.worker
    assert worker is not None

    service.indexer.hold()
    worker.queue_file("/repo/held.py")
    assert service.indexer.entered.wait(TIMEOUT), "the worker never began a commit"
    worker.queue_file("/repo/queued.py")

    try:
        report = resources.shutdown(timeout=0.2)
    finally:
        service.indexer.release()

    assert not report.completed
    assert normalize_file_identity("/repo/queued.py") in report.incomplete_work
    worker_stage = report.stage("worker")
    assert worker_stage is not None and not worker_stage.ok


def test_shutdown_is_bounded_by_its_timeout(resources_for) -> None:  # type: ignore[no-untyped-def]
    """A commit that never returns cannot hold the process open."""
    import time

    resources, service = resources_for()
    resources.startup()
    worker = resources.worker
    assert worker is not None

    service.indexer.hold()
    worker.queue_file("/repo/stuck.py")
    assert service.indexer.entered.wait(TIMEOUT), "the worker never began a commit"

    started = time.monotonic()
    try:
        report = resources.shutdown(timeout=0.3)
        elapsed = time.monotonic() - started
    finally:
        service.indexer.release()

    assert elapsed < TIMEOUT, f"shutdown ran for {elapsed:.2f}s despite a 0.3s budget"
    assert not report.completed


# ----------------------------------------------------------------------
# Idempotence and partial startup
# ----------------------------------------------------------------------


def test_shutdown_is_idempotent(resources_for, call_log) -> None:  # type: ignore[no-untyped-def]
    """A second shutdown closes nothing twice and still reports completion."""
    resources, service = resources_for()
    resources.startup()

    first = resources.shutdown(timeout=TIMEOUT)
    call_log.clear()
    second = resources.shutdown(timeout=TIMEOUT)

    assert first.completed and second.completed
    assert call_log == []
    assert service.closed == 1


def test_startup_is_idempotent(resources_for) -> None:  # type: ignore[no-untyped-def]
    """A second startup must not add a second worker over one queue."""
    resources, service = resources_for()

    resources.startup()
    worker = resources.worker
    resources.startup()

    assert resources.worker is worker
    assert service.indexer is service.watch_writer()


def test_shutdown_after_a_failed_startup_closes_what_exists(
    resources_for, call_log
) -> None:  # type: ignore[no-untyped-def]
    """A half-built server still releases everything it did build."""
    monitor = FakeMonitor(call_log)
    monitor.start_error = RuntimeError("watchdog could not schedule")
    resources, service = resources_for(monitor=monitor)

    with pytest.raises(RuntimeError):
        resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert service.closed == 1
    assert resources.worker is None or not resources.worker.is_running
    assert report.stage("stores") is not None


def test_shutdown_without_startup_is_safe(resources_for) -> None:  # type: ignore[no-untyped-def]
    """An app that never started must not raise on the way down."""
    resources, service = resources_for()

    report = resources.shutdown(timeout=TIMEOUT)

    assert report.completed
    assert service.closed == 0


# ----------------------------------------------------------------------
# Structured diagnostics
# ----------------------------------------------------------------------


def test_report_serializes_to_structured_diagnostics(resources_for) -> None:  # type: ignore[no-untyped-def]
    """Incomplete shutdown is machine-readable, not a prose log line."""
    resources, service = resources_for()
    service.close_error = RuntimeError("boom")
    resources.startup()

    payload = resources.shutdown(timeout=TIMEOUT).as_dict()

    assert payload["completed"] is False
    assert payload["failed_stages"] == ["stores"]
    assert isinstance(payload["stages"], list)
    assert {stage["name"] for stage in payload["stages"]} == set(SHUTDOWN_ORDER)


def test_report_of_a_clean_shutdown_claims_nothing_extra() -> None:
    """The empty report is completed with no work outstanding."""
    report = ShutdownReport(completed=True)

    assert report.completed
    assert report.incomplete_work == ()
    assert report.failed_stages == ()


def test_report_has_no_outcome_for_a_stage_that_never_ran() -> None:
    """Asking for an unknown stage returns nothing rather than inventing one."""
    assert ShutdownReport(completed=True).stage("monitor") is None


def test_shutdown_report_is_exposed_after_shutdown(resources_for) -> None:  # type: ignore[no-untyped-def]
    """The owner keeps its report so the app can surface it."""
    resources, _service = resources_for()
    resources.startup()
    assert resources.shutdown_report is None

    report = resources.shutdown(timeout=TIMEOUT)

    assert resources.shutdown_report is report


# ----------------------------------------------------------------------
# Misuse and stage-level failure
# ----------------------------------------------------------------------


def test_watch_without_a_root_is_rejected(call_log) -> None:  # type: ignore[no-untyped-def]
    """A watching server with nowhere to watch is a construction error."""
    with pytest.raises(ValueError, match="watch_root"):
        ServerResources(FakeService(call_log), watch=True)


def test_startup_after_shutdown_is_refused(resources_for) -> None:  # type: ignore[no-untyped-def]
    """Restarting closed resources would run a worker over closed stores."""
    resources, _service = resources_for()
    resources.startup()
    resources.shutdown(timeout=TIMEOUT)

    with pytest.raises(RuntimeError, match="shut down"):
        resources.startup()


def test_a_worker_that_fails_to_stop_is_reported(resources_for) -> None:  # type: ignore[no-untyped-def]
    """A drain that raises is a failed stage, not an escaped exception."""
    resources, _service = resources_for()
    resources.startup()

    def explode(timeout: float = 0.0) -> None:
        raise RuntimeError("queue is wedged")

    resources.worker.stop = explode  # type: ignore[method-assign]

    report = resources.shutdown(timeout=TIMEOUT)

    assert not report.completed
    stage = report.stage("worker")
    assert stage is not None and not stage.ok
    assert "queue is wedged" in (stage.detail or "")


def test_a_telemetry_drain_failure_is_reported(resources_for, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Telemetry that will not drain is named, not assumed durable."""
    from knowcode.api import lifecycle

    def explode(timeout: float = 0.0) -> bool:
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(lifecycle, "shutdown_telemetry", explode)
    resources, _service = resources_for()
    resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert not report.completed
    stage = report.stage("telemetry")
    assert stage is not None and "pool is gone" in (stage.detail or "")


def test_undrained_telemetry_is_reported(resources_for, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A telemetry drain that ran out of budget must not read as success."""
    from knowcode.api import lifecycle

    monkeypatch.setattr(lifecycle, "shutdown_telemetry", lambda timeout=0.0: False)
    resources, _service = resources_for()
    resources.startup()

    report = resources.shutdown(timeout=TIMEOUT)

    assert not report.completed
    stage = report.stage("telemetry")
    assert stage is not None and not stage.ok
    assert "not drained" in (stage.detail or "")
