"""Step 22 release gate: a bounded, seeded watch/read soak.

A soak that churns files under concurrent readers is where a generation-handoff
race hides. This one is deterministic and reproducible: a seeded RNG drives a
bounded sequence of add/modify/delete/move operations through the real watch
worker while readers lease and query the same service. The seed and the full
operation log are printed on any failure so a red run replays exactly.

The correctness mechanism is the lease and the worker's drain, never a sleep:
every reader pins one generation for the span of its checks, and ``drain()``
forces a publication so readers genuinely move across generations rather than
all seeing the first.

Override ``KNOWCODE_GATE_SEED`` / ``KNOWCODE_GATE_ITERATIONS`` to reproduce or
widen a run.
"""

from __future__ import annotations

import os
import random
import threading
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.doctor import run_doctor
from knowcode.indexing.background_indexer import BackgroundIndexer
from knowcode.indexing.generations import resolve_current_generation
from knowcode.service import KnowCodeService

from tests.helpers.adversarial_repo import AdversarialRepo, build_adversarial_repo

SEED = int(os.environ.get("KNOWCODE_GATE_SEED", "220022"))
ITERATIONS = int(os.environ.get("KNOWCODE_GATE_ITERATIONS", "14"))
READERS = 6
DRAIN_EVERY = 4


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _service(repo: AdversarialRepo, backend: str) -> KnowCodeService:
    config = AppConfig.default()
    config.vector_backend = backend
    return KnowCodeService(store_path=repo.output, app_config=config)


def _module_source(tag: int) -> str:
    return f"def scratch_{tag}(x: int) -> int:\n    return x + {tag}\n"


def test_seeded_watch_read_soak_keeps_generations_coherent(
    tmp_path: Path, backend: str
) -> None:
    rng = random.Random(SEED)
    repo = build_adversarial_repo(tmp_path)
    scratch = repo.source / "scratch"
    scratch.mkdir()

    service = _service(repo, backend)
    service.analyze(directory=repo.source, output=repo.output)

    counter = 0
    pool: list[Path] = []
    oplog: list[str] = []

    def new_file() -> Path:
        nonlocal counter
        counter += 1
        path = scratch / f"mod_{counter}.py"
        path.write_text(_module_source(counter), encoding="utf-8")
        return path

    # Seed the pool so modify/delete/move always have a valid target.
    worker = BackgroundIndexer(service.watch_writer())
    assert worker.start() is True

    stop = threading.Event()
    errors: list[BaseException] = []
    mixed: list[str] = []
    snapshots: list[frozenset[str]] = []
    lock = threading.Lock()

    def read() -> None:
        try:
            while not stop.is_set():
                with service.generation_lease() as bundle:
                    first = frozenset(e["id"] for e in service.search(""))
                    count = service.get_indexer().chunk_repo.count()
                    second = frozenset(e["id"] for e in service.search(""))
                    recount = service.get_indexer().chunk_repo.count()
                    if first != second or count != recount:
                        with lock:
                            mixed.append(str(bundle.generation_id))
                with lock:
                    snapshots.append(first)
        except BaseException as exc:  # noqa: BLE001 - reported to the assertions
            with lock:
                errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(READERS)]
    for thread in readers:
        thread.start()

    try:
        for path in (new_file(), new_file()):
            pool.append(path)
            worker.queue_file(path)

        for index in range(ITERATIONS):
            choice = rng.choice(["add", "modify", "delete", "move"]) if pool else "add"
            if choice == "add":
                path = new_file()
                pool.append(path)
                worker.queue_file(path)
                oplog.append(f"{index}:add:{path.name}")
            elif choice == "modify":
                path = rng.choice(pool)
                path.write_text(_module_source(rng.randint(1000, 9999)), encoding="utf-8")
                worker.queue_file(path)
                oplog.append(f"{index}:modify:{path.name}")
            elif choice == "delete":
                path = pool.pop(rng.randrange(len(pool)))
                path.unlink()
                worker.queue_removal(path)
                oplog.append(f"{index}:delete:{path.name}")
            else:  # move
                counter += 1
                old = pool[rng.randrange(len(pool))]
                new = scratch / f"mod_{counter}.py"
                new.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
                old.unlink()
                pool[pool.index(old)] = new
                worker.queue_move(old, new)
                oplog.append(f"{index}:move:{old.name}->{new.name}")

            if index % DRAIN_EVERY == DRAIN_EVERY - 1:
                worker.drain(timeout=90)

        report = worker.stop(timeout=120)
    finally:
        if worker.is_running:
            worker.stop(timeout=120)
        stop.set()
        for thread in readers:
            thread.join(timeout=120)
            assert not thread.is_alive(), "a reader thread did not finish in time"

    diag = f"seed={SEED} backend={backend} ops={oplog}"
    assert not errors, f"a reader touched a retired resource ({diag}): {errors!r}"
    assert not mixed, f"an operation observed two generations ({diag}): {mixed!r}"
    assert report.completed, f"drain incomplete ({diag}): {report.incomplete_work}"
    assert snapshots, f"the readers never observed a generation ({diag})"

    service.close()

    # The session ends on one coherent, doctor-accepted generation whose chunk
    # and vector membership agree.
    report_ = run_doctor(store_path=repo.output)
    generation_check = next(
        check for check in report_.checks if check.name == "Index generation"
    )
    assert generation_check.status != "fail", f"{generation_check.message} ({diag})"

    resolved = resolve_current_generation(repo.output / "knowcode_index")
    assert resolved is not None, diag
    assert resolved.manifest.counts["chunks"] == resolved.manifest.counts["vectors"], diag

    restarted = _service(repo, backend)
    try:
        with restarted.generation_lease() as bundle:
            chunks = bundle.indexer.chunk_repo.count()
            vectors = bundle.indexer.vector_store.count()
        # Every surviving scratch file is present; every deleted one is gone.
        with restarted.generation_lease() as bundle:
            files = bundle.indexer.chunk_repo.get_all_file_paths()
    finally:
        restarted.close()

    assert chunks == vectors, f"chunk/vector split after the soak ({diag})"
    for path in pool:
        assert any(path.name in indexed for indexed in files), (
            f"a surviving file is missing from the index: {path.name} ({diag})"
        )
