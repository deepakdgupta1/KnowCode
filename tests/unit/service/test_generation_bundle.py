"""Reader leases over one immutable generation bundle (Step 18).

The reviewed defect: service lazy initialization and ``reload()`` are unlocked,
and ``reload()`` refreshed only the knowledge store. Two threads racing first
use built two stores; a reload swapped components under a request that had
already started; and retired SQLite/vector resources were either closed beneath
an in-flight reader or dropped without ever being closed at all.

This module pins the primitive: an immutable bundle of one generation's
components, a reference-counted lease that keeps it alive for a whole
operation, and retirement that closes only after the last reader releases.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import pytest

from knowcode.generation_bundle import BundleSources, GenerationBundle


class FakeClosable:
    """A store-shaped resource that records whether it was closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    @property
    def is_closed(self) -> bool:
        return self.closed > 0


class FakeIndexer:
    """The narrow indexer surface a bundle closes."""

    def __init__(self, name: str = "indexer") -> None:
        self.chunk_repo = FakeClosable(f"{name}-chunks")
        self.vector_store = FakeClosable(f"{name}-vectors")
        self.embedding_provider = object()


def _sources(
    *,
    store: Optional[FakeClosable] = None,
    indexer: Optional[FakeIndexer] = None,
    on_store: Optional[Any] = None,
    on_indexer: Optional[Any] = None,
    on_engine: Optional[Any] = None,
) -> tuple[BundleSources, dict[str, Any]]:
    """Build bundle sources plus the components they hand out."""
    built = {
        "store": store or FakeClosable("store"),
        "indexer": indexer or FakeIndexer(),
        "engine": object(),
        "store_calls": 0,
        "indexer_calls": 0,
        "engine_calls": 0,
    }

    def open_store() -> Any:
        built["store_calls"] += 1
        if on_store is not None:
            on_store()
        return built["store"]

    def open_indexer() -> Any:
        built["indexer_calls"] += 1
        if on_indexer is not None:
            on_indexer()
        return built["indexer"]

    def open_search_engine(store_arg: Any, indexer_arg: Any) -> Any:
        built["engine_calls"] += 1
        if on_engine is not None:
            on_engine()
        return built["engine"]

    return (
        BundleSources(
            open_store=open_store,
            open_indexer=open_indexer,
            open_search_engine=open_search_engine,
        ),
        built,
    )


# ----------------------------------------------------------------------
# Materialization is lazy, single-flight, and failure-safe
# ----------------------------------------------------------------------


def test_components_are_opened_at_most_once() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    assert bundle.store is built["store"]
    assert bundle.store is built["store"]
    assert bundle.indexer is built["indexer"]
    assert bundle.search_engine is built["engine"]
    assert bundle.search_engine is built["engine"]

    assert built["store_calls"] == 1
    assert built["indexer_calls"] == 1
    assert built["engine_calls"] == 1


def test_nothing_is_opened_until_it_is_asked_for() -> None:
    """A bundle nobody reads must not open a SQLite connection."""
    sources, built = _sources()
    GenerationBundle(generation=None, sources=sources)

    assert built["store_calls"] == 0
    assert built["indexer_calls"] == 0


def test_concurrent_first_use_opens_one_store() -> None:
    """Two threads racing first use must not build two connections.

    The interleaving is forced rather than hoped for: the second reader is
    started from *inside* the first reader's opener, so it necessarily arrives
    while construction is in flight. A single-flight bundle leaves it blocked
    for the duration of the probe; an unlocked one lets it into the opener.
    """
    seen: list[Any] = []
    blocked_during_open: list[bool] = []
    started_second = threading.Event()

    def read() -> None:
        seen.append(bundle.store)

    second = threading.Thread(target=read)

    def on_store() -> None:
        if started_second.is_set():
            return
        started_second.set()
        second.start()
        # A bounded join: still alive means the second reader is waiting on the
        # construction lock, which is the whole contract under test.
        second.join(timeout=0.25)
        blocked_during_open.append(second.is_alive())

    sources, built = _sources(on_store=on_store)
    bundle = GenerationBundle(generation=None, sources=sources)

    first = threading.Thread(target=read)
    first.start()
    first.join(timeout=5)
    assert not first.is_alive()
    second.join(timeout=5)
    assert not second.is_alive()

    assert blocked_during_open == [True], "a second thread entered the opener"
    assert built["store_calls"] == 1
    assert len(seen) == 2
    assert seen[0] is seen[1]


def test_a_failed_open_leaves_the_bundle_retryable_and_unclosed() -> None:
    """A partial bundle is never published; the failure propagates."""
    attempts = {"n": 0}

    def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("chunks.db is locked")

    sources, built = _sources(on_indexer=flaky)
    bundle = GenerationBundle(generation=None, sources=sources)

    with pytest.raises(RuntimeError, match="locked"):
        _ = bundle.indexer

    assert bundle.indexer is built["indexer"]
    assert not bundle.is_closed


# ----------------------------------------------------------------------
# Leases and retirement
# ----------------------------------------------------------------------


def test_retirement_closes_immediately_when_no_reader_holds_the_bundle() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)
    _ = bundle.store
    _ = bundle.indexer

    bundle.retire()

    assert bundle.is_closed
    assert built["store"].closed == 1
    assert built["indexer"].chunk_repo.closed == 1
    assert built["indexer"].vector_store.closed == 1


def test_retirement_waits_for_the_last_reader_to_release() -> None:
    """A retired generation closes only after its final reader is done."""
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    assert bundle.acquire()
    assert bundle.acquire()
    _ = bundle.store

    bundle.retire()
    assert not bundle.is_closed, "a leased bundle must stay open"
    assert built["store"].closed == 0

    bundle.release()
    assert not bundle.is_closed, "one reader is still in flight"

    bundle.release()
    assert bundle.is_closed
    assert built["store"].closed == 1


def test_a_retired_bundle_refuses_new_leases() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)
    assert bundle.acquire()

    bundle.retire()

    assert bundle.acquire() is False
    bundle.release()
    assert bundle.is_closed


def test_close_is_idempotent() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)
    _ = bundle.store

    bundle.retire()
    bundle.retire()
    bundle.close()

    assert built["store"].closed == 1


def test_a_component_that_fails_to_close_does_not_skip_the_others() -> None:
    """Losing one connection must not leak every other one."""

    class Exploding(FakeClosable):
        def close(self) -> None:
            super().close()
            raise RuntimeError("connection already gone")

    indexer = FakeIndexer()
    indexer.chunk_repo = Exploding("chunks")
    sources, built = _sources(indexer=indexer)
    bundle = GenerationBundle(generation=None, sources=sources)
    _ = bundle.indexer
    _ = bundle.store

    bundle.retire()

    assert bundle.is_closed
    assert indexer.chunk_repo.closed == 1
    assert indexer.vector_store.closed == 1
    assert built["store"].closed == 1


def test_unopened_components_are_not_opened_just_to_close_them() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    bundle.retire()

    assert bundle.is_closed
    assert built["store_calls"] == 0
    assert built["indexer_calls"] == 0


def test_releasing_more_than_acquired_is_rejected() -> None:
    """An unbalanced release would close a bundle a reader still holds."""
    sources, _built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    with pytest.raises(RuntimeError, match="release"):
        bundle.release()


def test_a_reader_blocked_on_close_still_finishes_its_operation() -> None:
    """Retirement never closes a store out from under an in-flight read."""
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)
    assert bundle.acquire()
    store = bundle.store

    reading = threading.Event()
    retired = threading.Event()
    observed: list[bool] = []

    def read() -> None:
        reading.set()
        assert retired.wait(timeout=5)
        observed.append(store.is_closed)
        bundle.release()

    reader = threading.Thread(target=read)
    reader.start()
    assert reading.wait(timeout=5)

    bundle.retire()
    retired.set()
    reader.join(timeout=5)
    assert not reader.is_alive()

    assert observed == [False], "the reader saw its store closed mid-operation"
    assert bundle.is_closed
    assert built["store"].closed == 1


# ----------------------------------------------------------------------
# Identity and inspection
# ----------------------------------------------------------------------


class FakeGeneration:
    """The two generation facts a bundle exposes."""

    def __init__(self, generation_id: str, *, semantic: bool) -> None:
        self.generation_id = generation_id
        self.has_semantic_index = semantic


def test_a_bundle_reports_the_generation_it_reads() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(
        generation=FakeGeneration("g1", semantic=True), sources=sources  # type: ignore[arg-type]
    )

    assert bundle.generation_id == "g1"
    assert bundle.has_semantic_index


def test_a_graph_only_generation_reports_no_semantic_index() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(
        generation=FakeGeneration("g1", semantic=False), sources=sources  # type: ignore[arg-type]
    )

    assert not bundle.has_semantic_index


def test_a_flat_layout_bundle_has_no_generation_id() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    assert bundle.generation_id is None
    assert not bundle.has_semantic_index


def test_materialization_flags_track_what_was_opened() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    assert not bundle.has_store
    assert not bundle.has_indexer
    assert not bundle.has_search_engine

    _ = bundle.search_engine

    assert bundle.has_store
    assert bundle.has_indexer
    assert bundle.has_search_engine


def test_lease_count_and_retirement_are_observable() -> None:
    sources, _built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    assert bundle.lease_count == 0
    assert not bundle.is_retired

    assert bundle.acquire()
    assert bundle.lease_count == 1

    bundle.retire()
    assert bundle.is_retired
    assert not bundle.is_closed

    bundle.release()
    assert bundle.lease_count == 0
    assert bundle.is_closed


def test_a_component_without_close_is_left_alone() -> None:
    """The JSON knowledge store holds no connection and has no ``close``."""

    class Plain:
        pass

    plain = Plain()
    sources, _built = _sources(store=plain)  # type: ignore[arg-type]
    bundle = GenerationBundle(generation=None, sources=sources)
    _ = bundle.store

    bundle.retire()

    assert bundle.is_closed


def test_warm_opens_only_what_it_is_asked_for() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    bundle.warm(store=True)

    assert built["store_calls"] == 1
    assert built["indexer_calls"] == 0
    assert built["engine_calls"] == 0


def test_warm_opens_the_store_before_the_indexer() -> None:
    """A generation with no readable store is rejected before ``chunks.db``
    would be created beside it."""
    order: list[str] = []
    sources, built = _sources(
        on_store=lambda: order.append("store"),
        on_indexer=lambda: order.append("indexer"),
        on_engine=lambda: order.append("engine"),
    )
    bundle = GenerationBundle(generation=None, sources=sources)

    bundle.warm(search_engine=True)

    assert order == ["store", "indexer", "engine"]
    assert built["engine_calls"] == 1


def test_warm_propagates_the_failure_that_rejects_a_candidate() -> None:
    def explode() -> None:
        raise RuntimeError("knowledge.db is missing")

    sources, built = _sources(on_store=explode)
    bundle = GenerationBundle(generation=None, sources=sources)

    with pytest.raises(RuntimeError, match="missing"):
        bundle.warm(store=True, indexer=True)

    assert built["indexer_calls"] == 0, "an indexer was opened for a rejected bundle"


def test_warming_nothing_opens_nothing() -> None:
    sources, built = _sources()
    bundle = GenerationBundle(generation=None, sources=sources)

    bundle.warm()

    assert built["store_calls"] == 0
