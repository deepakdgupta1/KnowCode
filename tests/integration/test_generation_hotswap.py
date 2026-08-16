"""Generation hot-swap over a real repository and the real server (Step 18).

The unit suites pin the bundle primitive and the service wiring. These exercise
the assembled system: many concurrent readers over a multi-file tree while
generations are published underneath them, the watch worker committing across a
swap, the API answering a request while ``/reload`` runs, and ``knowcode
doctor`` still finding a coherent generation afterwards.

The invariant every case here asserts is the step's headline one: *every
request sees one complete generation.* A reader may see the old generation or
the new one; it must never see half of each, and it must never touch a resource
that retirement has closed.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowcode.api import api as api_module
from knowcode.api.main import create_app
from knowcode.config import AppConfig
from knowcode.doctor import run_doctor
from knowcode.indexing.background_indexer import BackgroundIndexer
from knowcode.indexing.generations import resolve_current_generation
from knowcode.service import KnowCodeService


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _repository(root: Path) -> Path:
    """A small multi-file, multi-language tree."""
    src = root / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "pkg" / "orders.py").write_text(
        '"""Order handling."""\n\n\n'
        "class OrderService:\n"
        '    """Place and cancel orders."""\n\n'
        "    def place(self, item: str) -> str:\n"
        '        """Place an order for an item."""\n'
        "        return validate(item)\n\n\n"
        "def validate(item: str) -> str:\n"
        '    """Validate an item name."""\n'
        "    return item.strip()\n",
        encoding="utf-8",
    )
    (src / "pkg" / "billing.py").write_text(
        "def charge(amount: int) -> int:\n"
        '    """Charge an amount."""\n'
        "    return amount\n",
        encoding="utf-8",
    )
    (src / "app.js").write_text(
        "export function boot() {\n  return 42;\n}\n", encoding="utf-8"
    )
    return src


def _entity_ids(service: KnowCodeService) -> frozenset[str]:
    return frozenset(entity["id"] for entity in service.search(""))


def _publish(root: Path, backend: str) -> str:
    """Publish a generation from the tree as it stands, via a second service."""
    builder = _service(root, backend)
    try:
        stats = builder.analyze(directory=root / "src", output=root)
    finally:
        builder.close()
    assert stats["published"] is True, stats
    return str(stats["generation_id"])


def _join(threads: list[threading.Thread], timeout: float = 60.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "a worker thread did not finish in time"


# ----------------------------------------------------------------------
# Concurrent readers across published generations
# ----------------------------------------------------------------------


def test_concurrent_readers_never_observe_a_mixed_generation(
    tmp_path: Path, backend: str
) -> None:
    """Eight readers looping while four generations publish underneath them."""
    src = _repository(tmp_path)
    reader_service = _service(tmp_path, backend)
    reader_service.analyze(directory=src, output=tmp_path)

    stop = threading.Event()
    errors: list[BaseException] = []
    mixed: list[str] = []
    observed: list[frozenset[str]] = []
    lock = threading.Lock()

    def read() -> None:
        try:
            while not stop.is_set():
                with reader_service.generation_lease() as bundle:
                    first = _entity_ids(reader_service)
                    chunks = reader_service.get_indexer().chunk_repo.count()
                    second = _entity_ids(reader_service)
                    again = reader_service.get_indexer().chunk_repo.count()
                    if first != second or chunks != again:
                        with lock:
                            mixed.append(str(bundle.generation_id))
                with lock:
                    observed.append(first)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            with lock:
                errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(8)]
    for thread in readers:
        thread.start()

    try:
        for index in range(4):
            (src / "pkg" / f"gen{index}.py").write_text(
                f"def gen{index}():\n    return {index}\n", encoding="utf-8"
            )
            _publish(tmp_path, backend)
            reader_service.reload()
    finally:
        stop.set()
        _join(readers)

    assert not errors, f"a reader touched a retired resource: {errors!r}"
    assert not mixed, f"an operation observed two generations: {mixed!r}"
    # The readers really did span generations rather than all seeing one.
    assert len(set(observed)) > 1
    assert reader_service.search("gen3")
    reader_service.close()


def test_a_hot_swapped_service_leaves_a_coherent_generation_on_disk(
    tmp_path: Path, backend: str
) -> None:
    """After swaps and retirement, doctor must still find one valid generation."""
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    _ = service.search("order")

    for index in range(3):
        (src / "pkg" / f"extra{index}.py").write_text(
            f"def extra{index}():\n    return {index}\n", encoding="utf-8"
        )
        _publish(tmp_path, backend)
        assert service.reload() is True

    service.close()

    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    assert resolved is not None
    report = run_doctor(store_path=tmp_path)
    generation_check = next(
        check for check in report.checks if check.name == "Index generation"
    )
    assert generation_check.status == "pass", generation_check.message
    for name in ("Knowledge store", "Semantic index", "Freshness"):
        check = next(item for item in report.checks if item.name == name)
        assert check.status != "fail", check.message


def test_a_reader_holding_a_generation_keeps_its_directory(
    tmp_path: Path, backend: str
) -> None:
    """Retention must not delete a generation a request is still reading.

    The rebuilds run through the *serving* service, which is how an in-process
    rebuild happens (``analyze()`` on the service answering queries, or the
    watch worker's owner). It passes its live generation ids to publication, so
    retention skips the one the reader holds. A publication from a separate
    process cannot see those leases; that limitation is pinned below.
    """
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    holding = threading.Event()
    published = threading.Event()
    survived: list[bool] = []
    answered: list[int] = []

    def read() -> None:
        with service.generation_lease() as bundle:
            assert bundle.generation is not None
            directory = bundle.generation.path
            holding.set()
            assert published.wait(timeout=60)
            survived.append(directory.is_dir())
            answered.append(len(_entity_ids(service)))

    reader = threading.Thread(target=read)
    reader.start()
    assert holding.wait(timeout=60)

    # Publish enough generations that plain retention would have removed the
    # one the reader holds.
    for index in range(3):
        (src / "pkg" / f"churn{index}.py").write_text(
            f"def churn{index}():\n    return {index}\n", encoding="utf-8"
        )
        service.analyze(directory=src, output=tmp_path)

    published.set()
    _join([reader])

    assert survived == [True], "a leased generation directory was retired"
    assert answered and answered[0] > 0
    service.close()


# ----------------------------------------------------------------------
# The watch worker across a swap
# ----------------------------------------------------------------------


def test_the_watch_worker_commits_into_the_generation_after_a_reload(
    tmp_path: Path, backend: str
) -> None:
    """The full worker, not just the writer handle, follows the swap."""
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    worker = BackgroundIndexer(service.watch_writer())
    assert worker.start() is True
    try:
        (src / "pkg" / "next.py").write_text(
            "def next_step():\n    return 1\n", encoding="utf-8"
        )
        _publish(tmp_path, backend)
        assert service.reload() is True
        current = service.current_generation()
        assert current is not None

        (src / "pkg" / "watched.py").write_text(
            "def watched():\n    return 1\n", encoding="utf-8"
        )
        worker.queue_file(src / "pkg" / "watched.py")
        report = worker.stop(timeout=30)
    finally:
        if worker.is_running:
            worker.stop(timeout=30)

    assert report.completed, report.incomplete_work
    with service.generation_lease() as bundle:
        indexed_files = bundle.indexer.chunk_repo.get_all_file_paths()
    assert any("watched.py" in path for path in indexed_files), (
        "the watched commit landed outside the current generation"
    )
    service.close()


def test_a_watch_session_leaves_a_generation_doctor_accepts(
    tmp_path: Path, backend: str
) -> None:
    """Step 18b's headline: a watch session ends on a coherent generation.

    Before it, the worker committed into the *published* generation's own
    ``chunks.db`` and vector artifacts, so one watched edit made the manifest
    stop describing what was on disk and ``knowcode doctor`` failed on it.
    """
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = resolve_current_generation(tmp_path / "knowcode_index")
    assert base is not None

    worker = BackgroundIndexer(service.watch_writer())
    assert worker.start() is True
    try:
        (src / "pkg" / "added.py").write_text(
            "def added():\n    return 1\n", encoding="utf-8"
        )
        (src / "pkg" / "billing.py").write_text(
            "def charge(amount: int) -> int:\n"
            '    """Charge an amount, twice."""\n'
            "    return amount * 2\n",
            encoding="utf-8",
        )
        (src / "app.js").unlink()
        worker.queue_file(src / "pkg" / "added.py")
        worker.queue_file(src / "pkg" / "billing.py")
        worker.queue_removal(src / "app.js")
        report = worker.stop(timeout=60)
    finally:
        if worker.is_running:
            worker.stop(timeout=60)

    assert report.completed, report.incomplete_work
    current = service.current_generation()
    assert current is not None
    assert current.generation_id != base.generation_id, (
        "the watch session never published a generation"
    )
    service.close()

    report_ = run_doctor(store_path=tmp_path)
    for name in ("Index generation", "Knowledge store", "Semantic index", "Freshness"):
        check = next(item for item in report_.checks if item.name == name)
        assert check.status != "fail", check.message

    restarted = _service(tmp_path, backend)
    try:
        with restarted.generation_lease() as bundle:
            files = bundle.indexer.chunk_repo.get_all_file_paths()
            chunks = bundle.indexer.chunk_repo.count()
            vectors = bundle.indexer.vector_store.count()
    finally:
        restarted.close()
    assert any("added.py" in path for path in files)
    assert not any("app.js" in path for path in files)
    assert chunks == vectors


# ----------------------------------------------------------------------
# Through the real server
# ----------------------------------------------------------------------


def test_a_retrieval_holds_one_generation_across_a_rebuild(
    tmp_path: Path, backend: str
) -> None:
    """``retrieve_context_for_query`` is the entry point CLI and MCP both use.

    It names chunk ids through retrieval and resolves them against the chunk
    repository afterwards. A rebuild landing between those two steps used to
    resolve one generation's ids against the next generation's chunks, so the
    misses silently shortened the answer.
    """
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    holding = threading.Event()
    rebuilt = threading.Event()
    observed: list[tuple[str, str]] = []
    errors: list[BaseException] = []

    def retrieve() -> None:
        try:
            with service.generation_lease() as bundle:
                before = str(bundle.generation_id)
                holding.set()
                assert rebuilt.wait(timeout=60)
                result = service.retrieve_context_for_query(
                    "how does placing an order work", max_tokens=1500
                )
                observed.append(
                    (before, str(service.current_generation().generation_id))
                )
                assert result["context_text"]
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    reader = threading.Thread(target=retrieve)
    reader.start()
    assert holding.wait(timeout=60)

    (src / "pkg" / "late.py").write_text(
        "def late():\n    return 1\n", encoding="utf-8"
    )
    _publish(tmp_path, backend)
    service.reload()
    rebuilt.set()
    _join([reader])

    assert not errors, f"the retrieval failed across the rebuild: {errors!r}"
    before, during = observed[0]
    assert before == during, "the retrieval crossed a generation boundary"
    service.close()


# ----------------------------------------------------------------------
# Through the real server
# ----------------------------------------------------------------------


def test_the_api_serves_requests_while_reload_runs(tmp_path: Path) -> None:
    """Reads and ``/reload`` racing over the real ASGI stack.

    ``POST /api/v1/context/query`` is deliberately not exercised here: it is
    unreachable on ``main`` too, because its body parameter is named
    ``request`` and SlowAPI's decorator rejects anything by that name that is
    not a ``starlette.requests.Request``. That is a rate-limiting wiring defect
    logged for Step 21, not a generation-handoff one.
    """
    src = _repository(tmp_path)
    builder = _service(tmp_path, "faiss")
    builder.analyze(directory=src, output=tmp_path)
    builder.close()

    app = create_app(store_path=str(tmp_path))
    errors: list[BaseException] = []
    statuses: list[tuple[str, int]] = []
    entity_counts: set[int] = set()
    lock = threading.Lock()
    stop = threading.Event()

    with TestClient(app) as client:

        def read() -> None:
            try:
                while not stop.is_set():
                    search = client.get("/api/v1/search", params={"q": "order"})
                    stats = client.get("/api/v1/stats")
                    with lock:
                        statuses.append(("search", search.status_code))
                        statuses.append(("stats", stats.status_code))
                        if stats.status_code == 200:
                            entity_counts.add(stats.json()["total_entities"])
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                with lock:
                    errors.append(exc)

        def reload_repeatedly() -> None:
            try:
                for index in range(3):
                    (src / "pkg" / f"api{index}.py").write_text(
                        f"def api{index}():\n    return {index}\n", encoding="utf-8"
                    )
                    _publish(tmp_path, "faiss")
                    response = client.post("/api/v1/reload")
                    with lock:
                        statuses.append(("reload", response.status_code))
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                with lock:
                    errors.append(exc)
            finally:
                stop.set()

        threads = [
            threading.Thread(target=read),
            threading.Thread(target=reload_repeatedly),
        ]
        for thread in threads:
            thread.start()
        _join(threads)

    assert not errors, f"the race raised: {errors!r}"
    assert statuses and {code for _, code in statuses} == {200}, statuses
    # The reloads really did land while requests were being served.
    assert len(entity_counts) > 1
    assert api_module._service is None, "the closed service stayed reachable"


def test_a_reader_in_another_process_is_not_protected_by_a_lease(
    tmp_path: Path,
) -> None:
    """The documented limit of in-process lease protection.

    Leases are process state, so a publication from a *different* process (or a
    second service instance, which stands in for one here) applies plain
    retention. The reader is not broken by that — its handles are already open
    — but the directory it holds can be removed underneath it, so a *new* read
    that needed to reopen the artifacts would not find them. ADR 4's
    one-writer-process-per-artifact-root rule is what keeps this narrow.
    """
    src = _repository(tmp_path)
    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)

    with service.generation_lease() as bundle:
        assert bundle.generation is not None
        held = bundle.generation.path
        pinned = _entity_ids(service)
        assert pinned

        for index in range(3):
            (src / "pkg" / f"other{index}.py").write_text(
                f"def other{index}():\n    return {index}\n", encoding="utf-8"
            )
            _publish(tmp_path, "faiss")

        # The already-open handles keep answering regardless of the directory.
        assert _entity_ids(service) == pinned

    moved = resolve_current_generation(tmp_path / "knowcode_index")
    assert moved is not None and moved.path != held, (
        "precondition: the pointer moved away from the generation held above"
    )
    service.close()
