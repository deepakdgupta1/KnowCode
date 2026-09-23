"""The FastAPI app's lifespan owns what it creates (Step 17).

These exercise the real ``create_app`` through a real ASGI lifespan, plus the
service-side ownership interface (``flush``/``close``) the lifespan drives.

Reproduced before Step 17, with production code and no fault injection: running
a complete lifespan over ``create_app(store_path=tmp, watch=True)`` left five
threads where four existed before, the worker still running, the observer still
alive, the chunk repository still open, and ``api._service`` still installed.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowcode.api import api
from knowcode.api.main import create_app
from knowcode.config import AppConfig
from knowcode.indexing import generations
from knowcode.service import KnowCodeService

TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _restore_global_service():  # type: ignore[no-untyped-def]
    """The module-level service is process state; never leak it between tests."""
    original = api._service
    yield
    api._service = original


def _repo(root: Path) -> Path:
    (root / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    return root


def _live_workers() -> int:
    """Count live watch-worker threads.

    Counted rather than name-diffed: every worker carries the same thread name,
    so a set difference silently passes once one worker has leaked.
    """
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "knowcode-watch-indexer" and thread.is_alive()
    )


def _config(backend: str = "lancedb") -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    config.embedding_models = []
    return config


def _service(root: Path, backend: str = "lancedb") -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


# ----------------------------------------------------------------------
# The wired app
# ----------------------------------------------------------------------


def test_lifespan_shutdown_releases_every_watch_resource(tmp_path: Path) -> None:
    """After the lifespan ends, no worker, observer, or thread outlives it."""
    _repo(tmp_path)
    before = _live_workers()

    app = create_app(store_path=str(tmp_path), watch=True)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        worker = app.state.bg_indexer
        assert worker.is_running
        assert _live_workers() == before + 1

    assert not worker.is_running
    assert app.state.monitor is None
    assert _live_workers() == before


def test_startup_defers_watch_threads_to_the_lifespan(tmp_path: Path) -> None:
    """An app that is created but never run must not start an observer."""
    _repo(tmp_path)
    before = _live_workers()

    app = create_app(store_path=str(tmp_path), watch=True)

    assert _live_workers() == before
    assert app.state.bg_indexer is None


def test_lifespan_shutdown_closes_the_stores_it_opened(tmp_path: Path) -> None:
    """The app created the repositories, so the app closes them."""
    _repo(tmp_path)
    app = create_app(store_path=str(tmp_path), watch=True)

    with TestClient(app):
        indexer = api._service.get_indexer()
        chunk_repo = indexer.chunk_repo

    assert chunk_repo.is_closed


def test_lifespan_shutdown_uninstalls_the_global_service(tmp_path: Path) -> None:
    """A closed service must not stay reachable by the next request."""
    _repo(tmp_path)
    app = create_app(store_path=str(tmp_path), watch=True)

    with TestClient(app):
        assert api._service is not None

    assert api._service is None


def test_repeated_startup_and_shutdown_leaves_no_residue(tmp_path: Path) -> None:
    """Two servers in one process must not inherit each other's resources."""
    _repo(tmp_path)
    before = _live_workers()

    for _ in range(2):
        app = create_app(store_path=str(tmp_path), watch=True)
        with TestClient(app) as client:
            assert client.get("/api/v1/health").status_code == 200
        assert not app.state.bg_indexer.is_running
        assert _live_workers() == before

    assert _live_workers() == before


def test_lifespan_shutdown_reports_incomplete_work(tmp_path: Path) -> None:
    """A drain that cannot finish is stated, never reported as clean."""
    _repo(tmp_path)
    app = create_app(store_path=str(tmp_path), watch=True, shutdown_timeout=0.2)

    entered = threading.Event()
    gate = threading.Event()
    worker = None

    try:
        with TestClient(app) as client:
            client.get("/api/v1/health")
            worker = app.state.bg_indexer
            # Patch the writer the worker drives, not the indexer underneath
            # it: ServiceWatchWriter holds its batch lock across the whole
            # commit, and shutdown's publish_pending() needs that same lock.
            # Parking the worker on the writer's method blocks it BEFORE the
            # lock is taken, keeping the drain live; parking it on the
            # indexer's method deadlocks shutdown against an untimed gate.
            writer = worker.indexer
            real_replace = writer.replace_file

            def slow_replace(path, **kwargs):  # type: ignore[no-untyped-def]
                entered.set()
                # No timeout: the test, not the clock, decides when work may
                # finish, so no CI stall can empty the queue before the drain
                # samples it. The finally below is the only thing that opens
                # the gate.
                gate.wait()
                return real_replace(path, **kwargs)

            writer.replace_file = slow_replace  # type: ignore[method-assign]
            worker.queue_file(tmp_path / "m.py")
            assert entered.wait(TIMEOUT), "the worker never began a commit"
            (tmp_path / "n.py").write_text(
                "def beta():\n    return 2\n", encoding="utf-8"
            )
            worker.queue_file(tmp_path / "n.py")
    finally:
        # The worker is parked on the gate inside the writer; open it even if
        # an assertion failed, or the blocked thread leaks into the next
        # test's live-worker count.
        gate.set()
        if worker is not None:
            worker.join(timeout=TIMEOUT)

    report = app.state.shutdown_report
    assert not report.completed
    assert report.incomplete_work


# ----------------------------------------------------------------------
# Service ownership interface
# ----------------------------------------------------------------------


def test_service_close_releases_its_stores(tmp_path: Path) -> None:
    """The service created the repositories, so the service closes them."""
    _repo(tmp_path)
    service = _service(tmp_path)
    service.analyze(directory=tmp_path, output=tmp_path)
    indexer = service.get_indexer()
    store = service.store

    service.close()

    assert indexer.chunk_repo.is_closed
    assert store.is_closed


def test_service_close_is_idempotent(tmp_path: Path) -> None:
    """Shutting down twice must not raise on an already-closed connection."""
    _repo(tmp_path)
    service = _service(tmp_path)
    service.get_indexer()
    assert not service.is_closed

    service.close()
    service.close()

    assert service.is_closed


def test_service_flush_persists_watched_vector_updates(tmp_path: Path) -> None:
    """A watch session's vectors must survive the process that made them.

    Reproduced before Step 17 with the FAISS backend: a watched commit left
    ``count() == 1`` in memory and nothing on disk, so a restart recovered 0
    vectors while the chunks were durable, the split-brain this plan exists to
    remove reintroduced at the process boundary.

    The assertion is on recovery, not on an artifact. The plane is rebuilt from
    the embeddings the chunk rows already carry, so a restart cannot lose it.
    """
    _repo(tmp_path)
    service = _service(tmp_path, backend="faiss")
    indexer = service.get_indexer()
    indexer.replace_file(tmp_path / "m.py")
    assert indexer.vector_store.count() == 1, (
        "precondition: the commit landed in memory"
    )

    service.flush()
    service.close()

    restarted = _service(tmp_path, backend="faiss")
    assert restarted.get_indexer().vector_store.count() == 1


def test_service_flush_never_edits_a_published_generation(tmp_path: Path) -> None:
    """A published generation is immutable (ADR 4); flushing must not rewrite it.

    The indexer is opened *after* the build, because that is what a watching
    server holds: a reader pointed straight at the published generation. Its
    artifacts must survive shutdown byte-for-byte, or the checksums recorded in
    the generation manifest stop describing what is on disk.
    """
    _repo(tmp_path)
    service = _service(tmp_path, backend="faiss")
    service.analyze(directory=tmp_path, output=tmp_path)
    generation = service.current_generation()
    assert generation is not None and generation.has_semantic_index

    indexer = service.get_indexer()
    assert indexer.chunk_repo.count() > 0, (
        "precondition: the reader is on the generation"
    )
    before = {
        p.name: p.stat().st_mtime_ns for p in generation.path.iterdir() if p.is_file()
    }

    service.flush()

    after = {
        p.name: p.stat().st_mtime_ns for p in generation.path.iterdir() if p.is_file()
    }
    assert after == before
    assert generations.validate_generation(generation.path, verify_digests=True) == []


def test_service_flush_without_an_indexer_is_a_no_op(tmp_path: Path) -> None:
    """Nothing was opened, so nothing needs flushing."""
    _repo(tmp_path)
    _service(tmp_path).flush()
