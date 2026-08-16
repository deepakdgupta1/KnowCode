"""Backend-neutral vector-store contract tests (Step 10, ADR 7).

Parametrized over the three backend modes:

* ``faiss``  — :class:`VectorStore` with the FAISS index installed
* ``numpy``  — :class:`VectorStore` with its numpy fallback (faiss monkeypatched
  away for the whole test, the same pattern as ``test_mock_vector_store``)
* ``lancedb`` — :class:`LanceDBVectorStore`, directory-backed so ``save()`` does
  not skip the artifact copy

The green core runs unchanged on every backend. The cases below it started as
strict xfails naming the precise backend/defect combinations that Steps 11
(VectorStore) and 12 (LanceDB) had to repair; each turned into an XPASS failure
the moment its step landed, which is what the strict marks were for. Both steps
have now landed, so every case is a live gate on every backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.vector_assertions import (
    assert_duplicate_add_is_prevented,
    assert_hostile_id_get_embedding_is_exact,
    assert_hostile_id_removal_is_exact,
    assert_hostile_id_upsert_is_exact,
    assert_native_tombstone_parity,
    assert_removed_id_does_not_consume_top_k_slot,
    assert_result_ids_unique_after_duplicate_add,
    run_vector_contract,
)

BACKENDS = ["faiss", "numpy", "lancedb"]


@pytest.fixture
def make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return ``build(backend_name)`` producing a fresh, isolated store.

    The numpy fallback needs ``vector_store.faiss`` held to ``None`` for the
    whole test because ``get_embedding``/``save``/``load``/``clear`` re-read the
    module global; the LanceDB path needs a unique directory per store so the
    contract cases do not accumulate state across factory calls.
    """
    counter = {"i": 0}

    def build(backend: str):
        if backend == "faiss":
            from knowcode.storage import vector_store

            if vector_store.faiss is None:  # type: ignore[attr-defined]
                pytest.skip("faiss not installed")
            from knowcode.storage.vector_store import VectorStore

            return VectorStore(dimension=2)
        if backend == "numpy":
            import knowcode.storage.vector_store as vs_module

            monkeypatch.setattr(vs_module, "faiss", None)
            from knowcode.storage.vector_store import VectorStore

            return VectorStore(dimension=2)
        if backend == "lancedb":
            from knowcode.storage.lancedb_vector_store import LanceDBVectorStore

            counter["i"] += 1
            return LanceDBVectorStore(
                dimension=2, path=str(tmp_path / f"db{counter['i']}")
            )
        raise ValueError(f"unknown backend: {backend}")

    return build


# --- Green core: every backend satisfies these today ---


@pytest.mark.parametrize("backend", BACKENDS)
def test_vector_contract_green_core(backend: str, make_store, tmp_path) -> None:
    """Run the always-green contract suite for one backend."""

    def factory():
        return make_store(backend)

    run_vector_contract(factory, tmp_path)


# --- Previously deferred defects, now live gates ---


# Native-tombstone parity and removed-ID slot consumption are VectorStore
# defects only (the ``.index.ntotal`` native row count). LanceDB has no such
# attribute, so it is excluded from these two cases; its durable-row parity is
# gated in ``test_lancedb_vector_store.py`` instead.


@pytest.mark.parametrize("backend", ["faiss", "numpy"])
def test_native_tombstone_parity(backend: str, make_store) -> None:
    assert_native_tombstone_parity(lambda: make_store(backend))


@pytest.mark.parametrize("backend", ["faiss", "numpy"])
def test_removed_id_does_not_consume_top_k_slot(backend: str, make_store) -> None:
    assert_removed_id_does_not_consume_top_k_slot(lambda: make_store(backend))


@pytest.mark.parametrize("backend", BACKENDS)
def test_duplicate_add_is_prevented(backend: str, make_store) -> None:
    assert_duplicate_add_is_prevented(lambda: make_store(backend))


@pytest.mark.parametrize("backend", BACKENDS)
def test_result_ids_unique_after_duplicate_add(backend: str, make_store) -> None:
    assert_result_ids_unique_after_duplicate_add(lambda: make_store(backend))


# Hostile-ID cases: VectorStore matches IDs exactly and was always green;
# LanceDB interpolated IDs into SQL-like filters until Step 12 replaced every
# predicate with a hex-digest exact key.


@pytest.mark.parametrize("backend", BACKENDS)
def test_hostile_id_removal_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_removal_is_exact(lambda: make_store(backend))


@pytest.mark.parametrize("backend", BACKENDS)
def test_hostile_id_get_embedding_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_get_embedding_is_exact(lambda: make_store(backend))


@pytest.mark.parametrize("backend", BACKENDS)
def test_hostile_id_upsert_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_upsert_is_exact(lambda: make_store(backend))


# --- close(): the retirement boundary added in Step 18 ---
#
# ``close()`` became a required protocol member once reader leases made closing
# a superseded generation's store safe. The contract is the same on every
# backend: buffered writes drain first (so a write that already returned is not
# lost by the close that follows it), the store is then empty rather than
# poisoned, and a repeat call does nothing.


@pytest.mark.parametrize("backend", BACKENDS)
def test_close_is_a_protocol_member(backend: str, make_store) -> None:
    from knowcode.protocols import VectorStoreProtocol

    store = make_store(backend)
    assert isinstance(store, VectorStoreProtocol)
    assert callable(store.close)


@pytest.mark.parametrize("backend", BACKENDS)
def test_close_drains_buffered_writes_before_releasing(
    backend: str, make_store, tmp_path: Path
) -> None:
    """A write that returned must be durable before the handle goes away."""
    store = make_store(backend)
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])
    store.save(tmp_path / "closing")

    store.close()

    reopened = make_store(backend)
    reopened.load(tmp_path / "closing")
    assert reopened.count() == 2
    assert sorted(chunk_id for chunk_id, _ in reopened.search([1.0, 0.0], limit=5)) == [
        "a",
        "b",
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_closed_store_is_empty_rather_than_poisoned(backend: str, make_store) -> None:
    """Shutdown diagnostics may still call ``count()``; it must not raise."""
    store = make_store(backend)
    store.add("a", [1.0, 0.0])

    store.close()

    assert store.count() == 0
    assert store.is_closed


@pytest.mark.parametrize("backend", BACKENDS)
def test_close_is_idempotent(backend: str, make_store) -> None:
    store = make_store(backend)
    store.add("a", [1.0, 0.0])

    store.close()
    store.close()

    assert store.is_closed
    assert store.count() == 0
