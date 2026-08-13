"""Shared, backend-neutral vector-store contract assertions.

Step 10 (ADR 7) freezes the :class:`~knowcode.protocols.VectorStoreProtocol`
contract. These helpers assert the parts that are green on every backend today
(dimension validation, exact-ID upsert, exact removal, live count, save/load,
empty-index search, unique result IDs). Backend-specific defects that Steps 11
and 12 repair are exercised by the separate, named strict-xfail helpers below.

Each helper receives a zero-argument ``factory`` that returns a *fresh, isolated*
store, so assertions never leak state between cases. A ``save_dir`` path is
required only for the round-trip case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from knowcode.errors import VectorDimensionError
from knowcode.protocols import VectorStoreProtocol

StoreFactory = Callable[[], VectorStoreProtocol]

# A hostile repository ID. Step 12 makes this exact-match data on LanceDB;
# today LanceDB interpolates it into a SQL-like filter, widening reads/deletes.
HOSTILE_ID = "x' OR true --"


def _ids(results: list[tuple[str, float]]) -> list[str]:
    return [chunk_id for chunk_id, _ in results]


def assert_dimension_rejected_on_add(factory: StoreFactory) -> None:
    """``add`` must raise VectorDimensionError at call time, not later."""
    store = factory()
    with pytest.raises(VectorDimensionError) as exc_info:
        store.add("bad", [1.0, 0.0, 0.0])  # dim 3 into a dim-2 store
    assert exc_info.value.expected == store.dimension
    assert exc_info.value.actual == 3


def assert_dimension_rejected_on_upsert(factory: StoreFactory) -> None:
    """``upsert`` must raise VectorDimensionError at call time, not later."""
    store = factory()
    store.add("a", [1.0, 0.0])
    with pytest.raises(VectorDimensionError):
        store.upsert("a", [1.0, 0.0, 0.0])


def assert_exact_id_upsert_is_idempotent(factory: StoreFactory) -> None:
    """Upserting one ID leaves exactly one live row carrying the new vector."""
    store = factory()
    store.add("a", [1.0, 0.0])
    store.upsert("a", [0.0, 1.0])
    assert store.count() == 1
    results = store.search([0.0, 1.0], limit=1)
    assert results and results[0][0] == "a"
    # Repeating the upsert keeps it at one row (true idempotency).
    store.upsert("a", [0.0, 1.0])
    assert store.count() == 1


def assert_removal_absent_at_top_middle_last(factory: StoreFactory) -> None:
    """Removing a top/middle/last-ranked ID removes it from result IDs."""
    vectors = {"a": [1.0, 0.0], "b": [0.7071, 0.7071], "c": [0.0, 1.0]}
    query = [1.0, 0.0]  # ranks a > b > c
    for target in ("a", "b", "c"):
        store = factory()
        for chunk_id, vec in vectors.items():
            store.add(chunk_id, vec)
        store.remove(target)
        assert target not in _ids(store.search(query, limit=3))


def assert_missing_id_removal_is_noop(factory: StoreFactory) -> None:
    """Removing an absent ID is a no-op (no error, count unchanged)."""
    store = factory()
    store.add("a", [1.0, 0.0])
    before = store.count()
    store.remove("does-not-exist")
    assert store.count() == before


def assert_live_count(factory: StoreFactory) -> None:
    """``count()`` tracks exactly the live, searchable IDs."""
    store = factory()
    assert store.count() == 0
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])
    assert store.count() == 2
    store.remove("a")
    assert store.count() == 1


def assert_save_load_roundtrip(factory: StoreFactory, save_dir: Path) -> None:
    """A saved index round-trips through load with its searchable IDs intact."""
    store = factory()
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])
    path = save_dir / "vectors"
    store.save(path)

    loaded = factory()
    loaded.load(path)
    results = loaded.search([1.0, 0.0], limit=1)
    assert results and results[0][0] == "c1"


def assert_flush_makes_buffered_writes_visible(factory: StoreFactory) -> None:
    """``flush()`` is callable and makes buffered writes searchable.

    On FAISS/NumPy this is a no-op (each add commits immediately); on LanceDB it
    drains the mutable buffer into the durable table. Either way a write is
    searchable after ``flush()`` returns.
    """
    store = factory()
    store.add("a", [1.0, 0.0])
    store.flush()
    assert store.count() == 1
    results = store.search([1.0, 0.0], limit=1)
    assert results and results[0][0] == "a"


def assert_empty_index_search_returns_empty(factory: StoreFactory) -> None:
    """An empty index returns ``[]`` from search (no spurious results)."""
    store = factory()
    assert store.search([1.0, 0.0], limit=5) == []


def assert_result_ids_unique_without_duplicates(factory: StoreFactory) -> None:
    """Distinct IDs yield unique result IDs (no duplication without cause)."""
    store = factory()
    for i, vec in enumerate(([1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071])):
        store.add(f"c{i}", vec)
    results = store.search([1.0, 0.0], limit=10)
    ids = _ids(results)
    assert len(ids) == len(set(ids))


def run_vector_contract(factory: StoreFactory, save_dir: Path) -> None:
    """Run every always-green contract case against a backend factory."""
    assert_dimension_rejected_on_add(factory)
    assert_dimension_rejected_on_upsert(factory)
    assert_exact_id_upsert_is_idempotent(factory)
    assert_removal_absent_at_top_middle_last(factory)
    assert_missing_id_removal_is_noop(factory)
    assert_live_count(factory)
    assert_save_load_roundtrip(factory, save_dir)
    assert_flush_makes_buffered_writes_visible(factory)
    assert_empty_index_search_returns_empty(factory)
    assert_result_ids_unique_without_duplicates(factory)


# --- Strict-xfail cases: desired contract that fails today (Steps 11/12) ---


def assert_native_tombstone_parity(factory: StoreFactory) -> None:
    """The native index row count equals the live count (no tombstone leak).

    VectorStore (FAISS and the numpy fallback) only deletes the ``id_map``
    entry on ``remove``; the dead row stays in the native index, so
    ``index.ntotal`` diverges from ``count()``. Fixed in Step 11.
    """
    store = factory()
    store.add("dead", [1.0, 0.0])
    store.add("live", [0.9, 0.1])
    store.remove("dead")
    assert store.index.ntotal == store.count()  # type: ignore[attr-defined]


def assert_removed_id_does_not_consume_top_k_slot(factory: StoreFactory) -> None:
    """A removed ID frees its slot so the next-best live result surfaces.

    VectorStore leaves the dead native row in place, so searching the removed
    vector's neighborhood with ``limit=1`` returns nothing instead of the
    next live result. Fixed in Step 11.
    """
    store = factory()
    store.add("dead", [1.0, 0.0])
    store.add("live", [0.9, 0.1])
    store.remove("dead")
    results = store.search([1.0, 0.0], limit=1)
    assert results and results[0][0] == "live"


def assert_duplicate_add_is_prevented(factory: StoreFactory) -> None:
    """Adding an existing ID again does not create a second live row.

    All three backends currently accept the duplicate (``count() == 2``).
    VectorStore is repaired in Step 11; LanceDB in Step 12.
    """
    store = factory()
    store.add("a", [1.0, 0.0])
    store.add("a", [0.0, 1.0])
    assert store.count() == 1


def assert_result_ids_unique_after_duplicate_add(factory: StoreFactory) -> None:
    """After a duplicate add, search result IDs are still unique.

    All three backends currently return ``[('a', ...), ('a', ...)]``.
    VectorStore is repaired in Step 11; LanceDB in Step 12.
    """
    store = factory()
    store.add("a", [1.0, 0.0])
    store.add("a", [0.0, 1.0])
    ids = _ids(store.search([1.0, 0.0], limit=5))
    assert len(ids) == len(set(ids))


def assert_hostile_id_removal_is_exact(factory: StoreFactory) -> None:
    """Removing a hostile ID removes only that row.

    LanceDB interpolates the ID into a SQL-like filter, so removing
    ``x' OR true --`` deletes every row. VectorStore matches IDs exactly and
    is green today. LanceDB is repaired in Step 12.
    """
    store = factory()
    store.add("safe", [0.0, 1.0])
    store.add(HOSTILE_ID, [1.0, 0.0])
    store.remove(HOSTILE_ID)
    assert store.count() == 1
    results = store.search([0.0, 1.0], limit=1)
    assert results and results[0][0] == "safe"


def assert_hostile_id_get_embedding_is_exact(factory: StoreFactory) -> None:
    """``get_embedding`` of a hostile ID returns that ID's own vector.

    LanceDB interpolates the ID, widening the read to another row. VectorStore
    matches IDs exactly and is green today. LanceDB is repaired in Step 12.
    """
    store = factory()
    store.add("safe", [0.0, 1.0])
    store.add(HOSTILE_ID, [1.0, 0.0])
    embedding = store.get_embedding(HOSTILE_ID)
    assert embedding is not None
    assert len(embedding) == 2
    assert abs(embedding[0] - 1.0) < 1e-6
    assert abs(embedding[1]) < 1e-6


def assert_hostile_id_upsert_is_exact(factory: StoreFactory) -> None:
    """Upserting a hostile ID touches only that row (inherits ``remove``).

    LanceDB upsert is ``remove`` then ``add``; the interpolated ``remove``
    wipes the other row. VectorStore is green today. LanceDB Step 12.
    """
    store = factory()
    store.add("safe", [0.0, 1.0])
    store.add(HOSTILE_ID, [1.0, 0.0])
    store.upsert(HOSTILE_ID, [1.0, 0.0])
    assert store.count() == 2
    results = store.search([0.0, 1.0], limit=1)
    assert results and results[0][0] == "safe"
