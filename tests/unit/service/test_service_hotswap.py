"""Service generation hot-swap and reader retirement (Step 18).

Reproduced against the pre-Step-18 service, using its production API only:

* ``KnowCodeService.store`` and ``get_indexer()`` are unlocked lazy fields, so
  two threads racing first use build two SQLite stores and one is leaked.
* A caller that has already started an operation sees ``reload()`` replace the
  store, indexer, and search engine underneath it, so one request can answer
  half from each generation.
* ``reload()`` drops retired stores instead of closing them, leaking a writer
  connection and a reader connection per reload.
* ``close()`` closes stores beneath an in-flight reader, which raises
  ``RepositoryClosedError`` in the middle of a request.
* A reload that cannot load the new generation leaves the service with no store
  at all, so a working service is destroyed by a failed refresh.
* The watch worker binds one indexer forever, so after a reload it keeps
  committing into the retired generation.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from knowcode.config import AppConfig
from knowcode.errors import RepositoryClosedError
from knowcode.indexing import generations
from knowcode.service import KnowCodeService


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    """Both vector backends must hand readers over the same way."""
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _write(root: Path, *names: str) -> Path:
    """Add one source file per name, leaving any others in place."""
    src = root / "src"
    src.mkdir(exist_ok=True)
    for name in names:
        (src / f"{name}.py").write_text(
            f'def {name}():\n    """The {name} routine."""\n    return 1\n',
            encoding="utf-8",
        )
    return src


def _remove(root: Path, *names: str) -> Path:
    """Delete named source files."""
    src = root / "src"
    for name in names:
        (src / f"{name}.py").unlink(missing_ok=True)
    return src


def _publish(root: Path, backend: str) -> str:
    """Publish a generation from the tree as it stands, via a second service."""
    builder = _service(root, backend)
    try:
        stats = builder.analyze(directory=root / "src", output=root)
    finally:
        builder.close()
    assert stats["published"] is True, stats
    generation_id = stats["generation_id"]
    assert isinstance(generation_id, str)
    return generation_id


def _destroy_index(root: Path) -> None:
    """Remove every artifact a reader could resolve a generation from."""
    index_root = root / "knowcode_index"
    generations.pointer_path(index_root).unlink(missing_ok=True)
    shutil.rmtree(generations.generations_dir(index_root), ignore_errors=True)


def _run(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target)
    thread.start()
    return thread


def _join(thread: threading.Thread, timeout: float = 30.0) -> None:
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "a worker thread did not finish in time"


def _names(service: KnowCodeService, *patterns: str) -> list[str]:
    """Entity names matching any pattern, sorted, for a stable comparison."""
    found: set[str] = set()
    for pattern in patterns:
        found.update(hit["name"] for hit in service.search(pattern))
    return sorted(found)


# ----------------------------------------------------------------------
# Task 1: concurrent first use
# ----------------------------------------------------------------------


def test_concurrent_first_use_opens_one_knowledge_store(
    tmp_path: Path, backend: str
) -> None:
    """Racing first use must be single-flight, not two connections."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    opened: list[Any] = []
    lock = threading.Lock()
    ready = threading.Barrier(4, timeout=10)

    def read() -> None:
        ready.wait()
        store = service.store
        with lock:
            opened.append(store)

    threads = [_run(read) for _ in range(4)]
    for thread in threads:
        _join(thread)

    assert len({id(store) for store in opened}) == 1
    service.close()


def test_concurrent_first_use_opens_one_indexer(tmp_path: Path, backend: str) -> None:
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    opened: list[Any] = []
    lock = threading.Lock()
    ready = threading.Barrier(4, timeout=10)

    def read() -> None:
        ready.wait()
        indexer = service.get_indexer()
        with lock:
            opened.append(indexer)

    threads = [_run(read) for _ in range(4)]
    for thread in threads:
        _join(thread)

    assert len({id(indexer) for indexer in opened}) == 1
    service.close()


# ----------------------------------------------------------------------
# Task 3: one stable bundle for a whole operation
# ----------------------------------------------------------------------


def test_a_leased_reader_keeps_one_generation_across_a_reload(
    tmp_path: Path, backend: str
) -> None:
    """A request that started on generation N finishes on generation N."""
    _write(tmp_path, "alpha", "beta")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    holding = threading.Event()
    swapped = threading.Event()
    observed: list[tuple[list[str], list[str]]] = []

    def read() -> None:
        with service.generation_lease():
            before = _names(service, "alpha", "beta")
            holding.set()
            assert swapped.wait(timeout=20)
            observed.append((before, _names(service, "alpha", "beta")))

    reader = _run(read)
    assert holding.wait(timeout=20)

    _remove(tmp_path, "beta")
    _publish(tmp_path, backend)
    service.reload()
    swapped.set()
    _join(reader)

    before, after = observed[0]
    assert before == after, "the reader's generation changed mid-operation"
    assert "beta" in before

    # And the swap did happen: a *new* operation sees the new generation.
    assert "beta" not in _names(service, "alpha", "beta")
    service.close()


def test_a_lease_pins_the_store_indexer_and_search_engine_together(
    tmp_path: Path, backend: str
) -> None:
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    with service.generation_lease() as bundle:
        store = service.store
        indexer = service.get_indexer()
        engine = service.get_search_engine()

        _write(tmp_path, "beta")
        _publish(tmp_path, backend)
        service.reload()

        assert service.store is store
        assert service.get_indexer() is indexer
        assert service.get_search_engine() is engine
        assert bundle.store is store

    assert service.store is not store
    service.close()


def test_nested_leases_resolve_to_one_bundle(tmp_path: Path, backend: str) -> None:
    """Service methods that lease internally must not shadow their caller."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    with service.generation_lease() as outer:
        with service.generation_lease() as inner:
            assert inner is outer
        # Leaving the inner lease must not retire the bundle the outer holds.
        assert not outer.is_closed
        assert service.store is outer.store

    service.close()


# ----------------------------------------------------------------------
# Task 4: retirement behind the last reader
# ----------------------------------------------------------------------


def test_a_reload_closes_the_retired_stores(tmp_path: Path, backend: str) -> None:
    """Retired SQLite resources are closed, not merely dropped."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    retired_store = service.store
    retired_repo = service.get_indexer().chunk_repo

    _write(tmp_path, "beta")
    _publish(tmp_path, backend)
    service.reload()

    assert retired_store.is_closed, "the retired knowledge store leaked"
    assert retired_repo.is_closed, "the retired chunk repository leaked"
    assert not service.store.is_closed
    service.close()


def test_retirement_waits_for_a_slow_reader(tmp_path: Path, backend: str) -> None:
    """A retired generation closes only after its last reader releases."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    holding = threading.Event()
    swapped = threading.Event()
    seen_closed: list[bool] = []

    def read() -> None:
        with service.generation_lease():
            store = service.store
            repo = service.get_indexer().chunk_repo
            holding.set()
            assert swapped.wait(timeout=20)
            seen_closed.append(store.is_closed or repo.is_closed)
            # A real read must still work while the swap is in flight.
            assert service.search("alpha")

    reader = _run(read)
    assert holding.wait(timeout=20)

    retired_store = service.store
    retired_repo = service.get_indexer().chunk_repo

    _write(tmp_path, "beta")
    _publish(tmp_path, backend)
    service.reload()
    assert not retired_store.is_closed, "closed under an in-flight reader"

    swapped.set()
    _join(reader)

    assert seen_closed == [False]
    assert retired_store.is_closed
    assert retired_repo.is_closed
    service.close()


def test_close_during_a_read_does_not_close_beneath_the_reader(
    tmp_path: Path, backend: str
) -> None:
    """Shutdown during a request must not raise inside it."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    holding = threading.Event()
    closed = threading.Event()
    errors: list[BaseException] = []

    def read() -> None:
        try:
            with service.generation_lease():
                holding.set()
                assert closed.wait(timeout=20)
                assert service.search("alpha")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    reader = _run(read)
    assert holding.wait(timeout=20)

    store = service.store
    service.close()
    closed.set()
    _join(reader)

    assert not errors, f"the reader failed mid-operation: {errors!r}"
    assert store.is_closed
    assert service.is_closed


def test_a_closed_service_refuses_new_leases(tmp_path: Path, backend: str) -> None:
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    _ = service.store
    service.close()

    with pytest.raises(RepositoryClosedError):
        with service.generation_lease():
            pass


# ----------------------------------------------------------------------
# Task 5: failure leaves the previous bundle serving
# ----------------------------------------------------------------------


def test_a_reload_onto_an_unloadable_generation_keeps_the_previous_one(
    tmp_path: Path, backend: str
) -> None:
    """A failed refresh must never destroy a working service."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    assert service.search("alpha")

    # Nothing a reader could resolve is left: no pointer, no generations, and
    # no flat fallback. Loading a replacement bundle cannot succeed.
    _destroy_index(tmp_path)

    assert service.reload() is False
    assert service.search("alpha"), "the previous generation stopped serving"
    assert not service.store.is_closed
    service.close()


def test_a_failed_reload_keeps_the_previous_generation_pointer(
    tmp_path: Path, backend: str
) -> None:
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    _ = service.store
    first = service.current_generation()
    assert first is not None

    _destroy_index(tmp_path)

    assert service.reload() is False
    current = service.current_generation()
    assert current is not None
    assert current.generation_id == first.generation_id
    service.close()


def test_repeated_reloads_are_stable(tmp_path: Path, backend: str) -> None:
    """Reloading with nothing new published must not churn resources."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    store = service.store

    assert service.reload() is False
    assert service.reload() is False

    assert service.store is store, "an unchanged generation was reopened"
    assert not store.is_closed
    service.close()


def test_reload_advances_once_per_published_generation(
    tmp_path: Path, backend: str
) -> None:
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    _ = service.store

    _write(tmp_path, "beta")
    second = _publish(tmp_path, backend)
    assert service.reload() is True
    current = service.current_generation()
    assert current is not None and current.generation_id == second
    assert service.reload() is False
    current = service.current_generation()
    assert current is not None and current.generation_id == second
    service.close()


def test_close_during_a_reload_leaves_nothing_open(
    tmp_path: Path, backend: str
) -> None:
    """Shutdown racing a reload closes both bundles exactly once."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    first_store = service.store
    _write(tmp_path, "beta")
    _publish(tmp_path, backend)

    started = threading.Barrier(2, timeout=20)
    errors: list[BaseException] = []

    def do_reload() -> None:
        try:
            started.wait()
            service.reload()
        except RepositoryClosedError:
            pass  # a reload that lost the race to shutdown is a clean outcome
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    def do_close() -> None:
        try:
            started.wait()
            service.close()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    threads = [_run(do_reload), _run(do_close)]
    for thread in threads:
        _join(thread)

    assert not errors, f"the race raised: {errors!r}"
    assert service.is_closed
    assert first_store.is_closed
    assert service.live_generation_ids() == ()


# ----------------------------------------------------------------------
# Task 6: real entry points select matching generations
# ----------------------------------------------------------------------


def test_analyze_moves_the_service_onto_the_generation_it_published(
    tmp_path: Path, backend: str
) -> None:
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    assert service.search("alpha")

    _write(tmp_path, "beta")
    stats = service.analyze(directory=src, output=tmp_path)

    current = service.current_generation()
    assert current is not None
    assert current.generation_id == stats["generation_id"]
    assert service.search("beta")
    assert service.get_indexer().chunk_repo.count() == stats["indexed_chunks"]
    service.close()


def test_analyze_retires_the_previous_bundle(tmp_path: Path, backend: str) -> None:
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    retired = service.store

    _write(tmp_path, "beta")
    service.analyze(directory=src, output=tmp_path)

    assert retired.is_closed
    assert not service.store.is_closed
    service.close()


def test_ensure_index_and_restart_select_the_same_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    current = service.current_generation()
    assert current is not None
    published = current.generation_id
    service.close()

    restarted = _service(tmp_path, backend)
    restarted.ensure_index(directory=src)

    resolved = restarted.current_generation()
    assert resolved is not None
    assert resolved.generation_id == published
    assert restarted.search("alpha")
    restarted.close()


def test_a_read_during_a_rebuild_sees_one_complete_generation(
    tmp_path: Path, backend: str
) -> None:
    """The step's headline invariant, through real entry points."""
    src = _write(tmp_path, "alpha", "beta")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    holding = threading.Event()
    rebuilt = threading.Event()
    observed: list[tuple[list[str], list[str]]] = []

    def read() -> None:
        with service.generation_lease():
            first = _names(service, "alpha", "beta")
            holding.set()
            assert rebuilt.wait(timeout=30)
            observed.append((first, _names(service, "alpha", "beta")))

    reader = _run(read)
    assert holding.wait(timeout=20)

    _remove(tmp_path, "beta")
    _publish(tmp_path, backend)
    service.reload()
    rebuilt.set()
    _join(reader)

    first, second = observed[0]
    assert first == second, "one operation observed two generations"
    assert "beta" in first
    service.close()


# ----------------------------------------------------------------------
# The watch writer follows the swap
# ----------------------------------------------------------------------


def test_the_watch_writer_commits_into_the_current_generation(
    tmp_path: Path, backend: str
) -> None:
    """A worker bound before a reload must not keep writing into the old one."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    writer = service.watch_writer()
    retired = service.current_generation()
    assert retired is not None

    _write(tmp_path, "beta")
    _publish(tmp_path, backend)
    assert service.reload() is True
    current = service.current_generation()
    assert current is not None
    assert current.generation_id != retired.generation_id

    _write(tmp_path, "gamma")
    writer.replace_file(src / "gamma.py")

    committed = generations.read_chunk_ids(current.chunks_db)
    assert any("gamma" in chunk_id for chunk_id in committed), (
        "the watch commit landed in the retired generation"
    )
    service.close()


def test_the_watch_writer_never_uses_a_closed_store(
    tmp_path: Path, backend: str
) -> None:
    """A reload racing a commit must not close the repository mid-transaction."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    writer = service.watch_writer()

    started = threading.Barrier(2, timeout=30)
    errors: list[BaseException] = []

    def commit() -> None:
        try:
            started.wait()
            for index in range(5):
                path = _write(tmp_path, f"w{index}") / f"w{index}.py"
                writer.replace_file(path)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    def swap() -> None:
        try:
            started.wait()
            for _ in range(3):
                _publish(tmp_path, backend)
                service.reload()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    threads = [_run(commit), _run(swap)]
    for thread in threads:
        _join(thread, timeout=120)

    assert not errors, f"a commit hit a retired resource: {errors!r}"
    service.close()


# ----------------------------------------------------------------------
# Writer surface and defensive boundaries
# ----------------------------------------------------------------------


def test_the_watch_writer_deletes_through_the_current_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _write(tmp_path, "alpha", "beta")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    writer = service.watch_writer()

    with service.generation_lease() as bundle:
        before = bundle.indexer.chunk_repo.get_all_file_paths()
    assert any("beta.py" in path for path in before)

    _remove(tmp_path, "beta")
    writer.delete_file(src / "beta.py")

    with service.generation_lease() as bundle:
        after = bundle.indexer.chunk_repo.get_all_file_paths()
    assert not any("beta.py" in path for path in after)
    service.close()


def test_a_swap_that_loses_the_race_to_close_is_refused(
    tmp_path: Path, backend: str
) -> None:
    """A candidate built while the service was closing is closed, not installed."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    candidate = service._open_bundle()
    candidate.warm(store=True)

    service.close()

    with pytest.raises(RepositoryClosedError):
        service._swap(candidate)
    assert candidate.is_closed, "a candidate for a closed service leaked"


def test_acquisition_gives_up_rather_than_spinning(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry bound turns a bug into an error, not a spinning request."""
    _write(tmp_path, "alpha")
    _publish(tmp_path, backend)
    service = _service(tmp_path, backend)

    retired = service._open_bundle()
    retired.retire()
    monkeypatch.setattr(service, "_live_bundle", lambda *a, **k: retired)

    with pytest.raises(RuntimeError, match="stable index generation"):
        with service.generation_lease():
            pass
    service.close()


def test_live_generation_ids_names_the_current_and_leased_generations(
    tmp_path: Path, backend: str
) -> None:
    _write(tmp_path, "alpha")
    first = _publish(tmp_path, backend)
    service = _service(tmp_path, backend)
    _ = service.store

    assert service.live_generation_ids() == (first,)

    holding = threading.Event()
    swapped = threading.Event()
    observed: list[tuple[str, ...]] = []

    def read() -> None:
        with service.generation_lease():
            _ = service.store
            holding.set()
            assert swapped.wait(timeout=20)
            observed.append(service.live_generation_ids())

    reader = _run(read)
    assert holding.wait(timeout=20)

    _write(tmp_path, "beta")
    second = _publish(tmp_path, backend)
    assert service.reload() is True
    swapped.set()
    _join(reader)

    assert observed[0] == (second, first), "a leased generation went unreported"
    assert service.live_generation_ids() == (second,)
    service.close()


def test_stats_report_the_leased_generation(tmp_path: Path, backend: str) -> None:
    """Statistics describe one generation, and never open an indexer to do it."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    with service.generation_lease() as bundle:
        cold = service.get_stats()
        assert not bundle.has_indexer, "get_stats opened a chunk repository"
        assert cold["generation_id"] == bundle.generation_id
        assert "total_chunks" not in cold

        service.get_indexer()
        warm = service.get_stats()

    assert warm["total_chunks"] == bundle.indexer.chunk_repo.count()
    assert warm["vector_index_size"] == bundle.indexer.vector_store.count()
    assert warm["total_entities"] == cold["total_entities"]
    service.close()


def test_graph_edges_are_read_from_one_generation(
    tmp_path: Path, backend: str
) -> None:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "graph.py").write_text(
        "def leaf():\n    return 1\n\n\ndef root():\n    return leaf()\n",
        encoding="utf-8",
    )
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    root = next(hit for hit in service.search("root") if hit["kind"] == "function")
    leaf = next(hit for hit in service.search("leaf") if hit["kind"] == "function")

    assert [callee["id"] for callee in service.get_callees(root["id"])] == [leaf["id"]]
    assert [caller["id"] for caller in service.get_callers(leaf["id"])] == [root["id"]]
    service.close()
