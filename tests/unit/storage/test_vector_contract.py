"""Backend-neutral vector-store contract tests (Step 10, ADR 7).

Parametrized over the three backend modes:

* ``faiss``  — :class:`VectorStore` with the FAISS index installed
* ``numpy``  — :class:`VectorStore` with its numpy fallback (faiss monkeypatched
  away for the whole test, the same pattern as ``test_mock_vector_store``)
* ``lancedb`` — :class:`LanceDBVectorStore`, directory-backed so ``save()`` does
  not skip the artifact copy

The green core runs unchanged on every backend. The strict-xfail cases name the
precise backend/defect combinations that Steps 11 (VectorStore) and 12 (LanceDB)
repair; each carries an explicit reason and turns into a failure (XPASS) the
moment a later step fixes the behavior, so the xfail cannot rot.
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
            return LanceDBVectorStore(dimension=2, path=str(tmp_path / f"db{counter['i']}"))
        raise ValueError(f"unknown backend: {backend}")

    return build


# --- Green core: every backend satisfies these today ---


@pytest.mark.parametrize("backend", BACKENDS)
def test_vector_contract_green_core(backend: str, make_store, tmp_path) -> None:
    """Run the always-green contract suite for one backend."""

    def factory():
        return make_store(backend)

    run_vector_contract(factory, tmp_path)


# --- Strict-xfail cases ---


# Native-tombstone parity and removed-ID slot consumption are VectorStore
# defects only (the ``.index.ntotal`` native row count). LanceDB has no such
# attribute, so it is excluded from these two cases. Step 11 repaired both
# VectorStore engines, so they are live gates here rather than xfails.


@pytest.mark.parametrize("backend", ["faiss", "numpy"])
def test_native_tombstone_parity(backend: str, make_store) -> None:
    assert_native_tombstone_parity(lambda: make_store(backend))


@pytest.mark.parametrize("backend", ["faiss", "numpy"])
def test_removed_id_does_not_consume_top_k_slot(backend: str, make_store) -> None:
    assert_removed_id_does_not_consume_top_k_slot(lambda: make_store(backend))


@pytest.mark.parametrize(
    "backend",
    [
        "faiss",
        "numpy",
        pytest.param(
            "lancedb",
            marks=pytest.mark.xfail(
                strict=True,
                reason="LanceDB accepts duplicate IDs (count==2). Step 12.",
            ),
        ),
    ],
)
def test_duplicate_add_is_prevented(backend: str, make_store) -> None:
    assert_duplicate_add_is_prevented(lambda: make_store(backend))


@pytest.mark.parametrize(
    "backend",
    [
        "faiss",
        "numpy",
        pytest.param(
            "lancedb",
            marks=pytest.mark.xfail(
                strict=True,
                reason="LanceDB returns duplicate result IDs. Step 12.",
            ),
        ),
    ],
)
def test_result_ids_unique_after_duplicate_add(backend: str, make_store) -> None:
    assert_result_ids_unique_after_duplicate_add(lambda: make_store(backend))


# Hostile-ID cases: VectorStore matches IDs exactly (green); LanceDB interpolates
# IDs into SQL-like filters and widens the operation (xfail, Step 12).


@pytest.mark.parametrize(
    "backend",
    [
        "faiss",
        "numpy",
        pytest.param(
            "lancedb",
            marks=pytest.mark.xfail(
                strict=True,
                reason="LanceDB interpolated filter widens removal. Step 12.",
            ),
        ),
    ],
)
def test_hostile_id_removal_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_removal_is_exact(lambda: make_store(backend))


@pytest.mark.parametrize(
    "backend",
    [
        "faiss",
        "numpy",
        pytest.param(
            "lancedb",
            marks=pytest.mark.xfail(
                strict=True,
                reason="LanceDB interpolated filter widens get_embedding. Step 12.",
            ),
        ),
    ],
)
def test_hostile_id_get_embedding_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_get_embedding_is_exact(lambda: make_store(backend))


@pytest.mark.parametrize(
    "backend",
    [
        "faiss",
        "numpy",
        pytest.param(
            "lancedb",
            marks=pytest.mark.xfail(
                strict=True,
                reason="LanceDB upsert inherits the interpolated remove. Step 12.",
            ),
        ),
    ],
)
def test_hostile_id_upsert_is_exact(backend: str, make_store) -> None:
    assert_hostile_id_upsert_is_exact(lambda: make_store(backend))
