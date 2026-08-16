"""Step 11 contract tests for the FAISS/NumPy :class:`VectorStore`.

The reviewed defect: ``remove()`` deleted only the ``id_map`` entry, so the
dead vector stayed in the native index. ``count()`` disagreed with
``index.ntotal``, a removed ID consumed a top-k slot (top-1 search returned no
live result), and a duplicate ``add`` produced two native rows for one chunk ID.

Every case runs against both engines of the one ``VectorStore`` class: FAISS
when installed, and the numpy fallback with ``vector_store.faiss`` monkeypatched
to ``None`` for the whole test (``clear``/``save``/``load`` re-read the module
global, so the patch must outlive construction).
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

import pytest

from knowcode.errors import VectorArtifactVersionError, VectorDimensionError
from knowcode.storage import vector_store as vector_store_module
from knowcode.storage.vector_store import VectorStore
from knowcode.utils import atomic_write
from knowcode.utils.atomic_write import TEMP_SUFFIX

ENGINES = ["faiss", "numpy"]


@pytest.fixture(params=ENGINES)
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Select the native engine for one test and return its name."""
    if request.param == "faiss":
        if vector_store_module.faiss is None:
            pytest.skip("faiss not installed")
    else:
        monkeypatch.setattr(vector_store_module, "faiss", None)
    return str(request.param)


@pytest.fixture
def store(engine: str) -> VectorStore:
    """A fresh two-dimensional store on the selected engine."""
    return VectorStore(dimension=2)


def _ids(results: list[tuple[str, float]]) -> list[str]:
    return [chunk_id for chunk_id, _ in results]


def _native_artifact(path: Path, engine: str) -> Path:
    return path.with_suffix(".index" if engine == "faiss" else ".npy")


def _read_metadata(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        path.with_suffix(".json").read_text(encoding="utf-8")
    )
    return payload


def _write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")


def _saved_store(store: VectorStore, path: Path) -> Path:
    """Persist a small populated index and return its base path."""
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])
    store.save(path)
    return path


# --- Native removal: no tombstones ---------------------------------------


def test_removed_id_leaves_no_dead_native_row(store: VectorStore) -> None:
    """``count()`` equals the native row count after a removal."""
    store.add("dead", [1.0, 0.0])
    store.add("live", [0.9, 0.1])

    store.remove("dead")

    assert store.count() == 1
    assert store.index.ntotal == 1


def test_removed_top_ranked_id_frees_its_top_k_slot(store: VectorStore) -> None:
    """The reproduced defect: top-1 search returned no live result."""
    store.add("dead", [1.0, 0.0])
    store.add("live", [0.9, 0.1])

    store.remove("dead")

    results = store.search([1.0, 0.0], limit=1)
    assert _ids(results) == ["live"]


def test_removed_id_has_no_embedding(store: VectorStore) -> None:
    """A removed ID is unreadable, not merely unlisted."""
    store.add("dead", [1.0, 0.0])
    store.remove("dead")

    assert store.get_embedding("dead") is None


@pytest.mark.parametrize("target", ["a", "b", "c"])
def test_removal_at_top_middle_and_last_rank_leaves_no_tombstone(
    store: VectorStore, target: str
) -> None:
    """Removing any ranked position keeps native rows and IDs in sync."""
    store.add("a", [1.0, 0.0])
    store.add("b", [0.7071, 0.7071])
    store.add("c", [0.0, 1.0])

    store.remove(target)

    assert store.count() == 2
    assert store.index.ntotal == 2
    remaining = _ids(store.search([1.0, 0.0], limit=3))
    assert target not in remaining
    assert len(remaining) == 2


def test_removing_absent_id_is_a_noop(store: VectorStore) -> None:
    """Removing an ID that was never added changes nothing."""
    store.add("a", [1.0, 0.0])

    store.remove("missing")

    assert store.count() == 1
    assert store.index.ntotal == 1


# --- Exact-ID upsert ------------------------------------------------------


def test_duplicate_add_replaces_the_live_row(store: VectorStore) -> None:
    """``add`` of an existing ID replaces it instead of duplicating it."""
    store.add("a", [1.0, 0.0])
    store.add("a", [0.0, 1.0])

    assert store.count() == 1
    assert store.index.ntotal == 1
    embedding = store.get_embedding("a")
    assert embedding is not None
    assert embedding == pytest.approx([0.0, 1.0])


def test_duplicate_add_cannot_bias_search_results(store: VectorStore) -> None:
    """One ID cannot occupy two result slots and double its fusion weight."""
    store.add("a", [1.0, 0.0])
    store.add("a", [1.0, 0.0])

    ids = _ids(store.search([1.0, 0.0], limit=5))
    assert ids == ["a"]


def test_upsert_leaves_exactly_one_native_row(store: VectorStore) -> None:
    """Repeated upserts of one ID never accumulate native rows."""
    store.add("a", [1.0, 0.0])
    for _ in range(5):
        store.upsert("a", [0.0, 1.0])

    assert store.count() == 1
    assert store.index.ntotal == 1


def test_upsert_of_a_new_id_adds_it(store: VectorStore) -> None:
    """Upsert is add-or-replace, not replace-only."""
    store.upsert("fresh", [1.0, 0.0])

    assert _ids(store.search([1.0, 0.0], limit=1)) == ["fresh"]


@pytest.mark.parametrize("operation", ["add", "upsert"])
def test_dimension_mismatch_is_rejected_at_call_time(
    store: VectorStore, operation: str
) -> None:
    """A wrong-length embedding raises before touching the native index."""
    with pytest.raises(VectorDimensionError) as exc_info:
        getattr(store, operation)("bad", [1.0, 0.0, 0.0])

    assert exc_info.value.expected == 2
    assert exc_info.value.actual == 3
    assert store.count() == 0
    assert store.index.ntotal == 0


# --- Clear and empty-index behavior ---------------------------------------


def test_clear_drops_native_rows_and_ids(store: VectorStore) -> None:
    """``clear`` empties the native index, not just the ID map."""
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])

    store.clear()

    assert store.count() == 0
    assert store.index.ntotal == 0
    assert store.search([1.0, 0.0], limit=5) == []


def test_search_on_empty_index_returns_no_results(store: VectorStore) -> None:
    """An empty index yields ``[]`` rather than padded misses."""
    assert store.search([1.0, 0.0], limit=5) == []


def test_search_with_non_positive_limit_returns_no_results(store: VectorStore) -> None:
    """A zero/negative limit is answered without calling the native index."""
    store.add("a", [1.0, 0.0])

    assert store.search([1.0, 0.0], limit=0) == []
    assert store.search([1.0, 0.0], limit=-1) == []


# --- Persistence: no ID-map drift -----------------------------------------


def test_save_load_roundtrip_preserves_ids_after_removal(
    store: VectorStore, engine: str, tmp_path: Path
) -> None:
    """A removal survives persistence with counts and IDs still in sync."""
    store.add("a", [1.0, 0.0])
    store.add("b", [0.7071, 0.7071])
    store.add("c", [0.0, 1.0])
    store.remove("b")
    path = tmp_path / "vectors"
    store.save(path)

    loaded = VectorStore(dimension=2)
    loaded.load(path)

    assert loaded.count() == 2
    assert loaded.index.ntotal == 2
    assert sorted(_ids(loaded.search([1.0, 0.0], limit=5))) == ["a", "c"]
    embedding = loaded.get_embedding("a")
    assert embedding is not None
    assert embedding == pytest.approx([1.0, 0.0])
    assert loaded.get_embedding("b") is None


def test_reloaded_store_keeps_upsert_exact(store: VectorStore, tmp_path: Path) -> None:
    """Native ID assignment resumes after load without colliding."""
    path = _saved_store(store, tmp_path / "vectors")

    loaded = VectorStore(dimension=2)
    loaded.load(path)
    loaded.upsert("c1", [0.0, 1.0])
    loaded.add("c3", [0.7071, 0.7071])

    assert loaded.count() == 3
    assert loaded.index.ntotal == 3
    ids = _ids(loaded.search([0.0, 1.0], limit=5))
    assert len(ids) == len(set(ids))


def test_saved_metadata_records_the_contract_envelope(
    store: VectorStore, engine: str, tmp_path: Path
) -> None:
    """Backend, engine, dimension, live count, generation, and version persist."""
    path = _saved_store(store, tmp_path / "vectors")

    payload = _read_metadata(path)

    assert payload["schema_version"] == VectorStore.SCHEMA_VERSION
    assert payload["backend"] == "faiss"
    assert payload["engine"] == engine
    assert payload["dimension"] == 2
    assert payload["count"] == 2
    assert isinstance(payload["generation"], int)
    assert set(payload["id_map"].values()) == {"c1", "c2"}


def test_load_is_a_noop_when_no_artifact_exists(
    store: VectorStore, tmp_path: Path
) -> None:
    """A never-saved index loads as empty (``Indexer.load`` relies on this)."""
    store.load(tmp_path / "vectors")

    assert store.count() == 0
    assert store.index.ntotal == 0


@pytest.mark.parametrize("bad_version", [1, 2, 999, "abc", None])
def test_load_rejects_unsupported_metadata_version(
    store: VectorStore, tmp_path: Path, bad_version: Any
) -> None:
    """Legacy, malformed, and newer envelopes fail closed with rebuild guidance."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    if bad_version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = bad_version
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        loaded.load(path)


def test_load_rejects_engine_mismatch(
    store: VectorStore, engine: str, tmp_path: Path
) -> None:
    """An artifact written by the other engine is rejected, not half-loaded."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["engine"] = "numpy" if engine == "faiss" else "faiss"
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="engine"):
        loaded.load(path)


def test_load_rejects_dimension_mismatch(store: VectorStore, tmp_path: Path) -> None:
    """Metadata dimension must agree with the persisted native index."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["dimension"] = 3
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="dimension"):
        loaded.load(path)


def test_load_rejects_id_map_and_native_count_drift(
    store: VectorStore, tmp_path: Path
) -> None:
    """An ID map that lost an entry cannot silently shrink the live count."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["id_map"].popitem()
    payload["count"] = len(payload["id_map"])
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="count"):
        loaded.load(path)


def test_load_rejects_duplicate_chunk_ids_in_id_map(
    store: VectorStore, tmp_path: Path
) -> None:
    """Two native rows claiming one chunk ID is drift, not a live duplicate."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["id_map"] = {key: "c1" for key in payload["id_map"]}
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="duplicate"):
        loaded.load(path)


def test_load_rejects_metadata_without_its_native_artifact(
    store: VectorStore, engine: str, tmp_path: Path
) -> None:
    """A half-published generation fails closed instead of loading empty."""
    path = _saved_store(store, tmp_path / "vectors")
    _native_artifact(path, engine).unlink()

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        loaded.load(path)


def test_load_rejects_native_artifact_without_its_metadata(
    store: VectorStore, tmp_path: Path
) -> None:
    """Vectors with no envelope cannot be adopted with a guessed ID map."""
    path = _saved_store(store, tmp_path / "vectors")
    path.with_suffix(".json").unlink()

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        loaded.load(path)


def test_load_rejects_metadata_that_is_not_an_object(
    store: VectorStore, tmp_path: Path
) -> None:
    """A JSON array where the envelope belongs is malformed, not empty."""
    path = _saved_store(store, tmp_path / "vectors")
    path.with_suffix(".json").write_text(json.dumps([1, 2]), encoding="utf-8")

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="JSON object"):
        loaded.load(path)


def test_load_rejects_id_map_that_is_not_an_object(
    store: VectorStore, tmp_path: Path
) -> None:
    """``id_map`` must be a mapping; a list cannot describe native IDs."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["id_map"] = ["c1", "c2"]
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="id_map"):
        loaded.load(path)


def test_load_rejects_unreadable_id_map_keys(
    store: VectorStore, tmp_path: Path
) -> None:
    """Native IDs are integers; a non-numeric key is unusable."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["id_map"] = {"not-an-int": "c1"}
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="id_map"):
        loaded.load(path)


def test_load_rejects_non_integer_dimension(store: VectorStore, tmp_path: Path) -> None:
    """A textual dimension cannot be compared against the native index."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["dimension"] = "two"
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="dimension"):
        loaded.load(path)


def test_load_rejects_declared_count_that_disagrees_with_the_id_map(
    store: VectorStore, tmp_path: Path
) -> None:
    """The declared live count must match the mapping it summarizes."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["count"] = 99
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="count"):
        loaded.load(path)


def test_numpy_engine_load_rejects_missing_row_ids(tmp_path: Path) -> None:
    """The numpy artifact carries no IDs of its own, so row_ids is required."""
    import knowcode.storage.vector_store as module

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "faiss", None)
        store = VectorStore(dimension=2)
        path = _saved_store(store, tmp_path / "vectors")
        payload = _read_metadata(path)
        payload.pop("row_ids")
        _write_metadata(path, payload)

        loaded = VectorStore(dimension=2)
        with pytest.raises(VectorArtifactVersionError, match="row_ids"):
            loaded.load(path)


def test_numpy_engine_load_rejects_row_ids_of_the_wrong_length(
    tmp_path: Path,
) -> None:
    """row_ids that cannot cover every saved row is drift, not a partial load."""
    import knowcode.storage.vector_store as module

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "faiss", None)
        store = VectorStore(dimension=2)
        path = _saved_store(store, tmp_path / "vectors")
        payload = _read_metadata(path)
        payload["row_ids"] = payload["row_ids"][:1]
        _write_metadata(path, payload)

        loaded = VectorStore(dimension=2)
        with pytest.raises(VectorArtifactVersionError, match="row_ids"):
            loaded.load(path)


def test_constructor_loads_a_saved_index_from_its_base_path(
    store: VectorStore, tmp_path: Path
) -> None:
    """``index_path`` loads the artifacts saved beside that base path."""
    path = _saved_store(store, tmp_path / "vectors")

    loaded = VectorStore(dimension=2, index_path=path)

    assert loaded.count() == 2
    assert _ids(loaded.search([1.0, 0.0], limit=1)) == ["c1"]


def test_constructor_with_an_unsaved_path_starts_empty(tmp_path: Path) -> None:
    """A base path with no artifacts is a fresh index, not an error."""
    store = VectorStore(dimension=2, index_path=tmp_path / "vectors")

    assert store.count() == 0


def test_load_replaces_prior_state(store: VectorStore, tmp_path: Path) -> None:
    """Loading is a replacement, so stale in-memory IDs cannot survive it."""
    path = _saved_store(store, tmp_path / "vectors")

    loaded = VectorStore(dimension=2)
    loaded.add("stale", [0.7071, 0.7071])
    loaded.load(path)

    assert loaded.count() == 2
    assert loaded.index.ntotal == 2
    assert loaded.get_embedding("stale") is None


def test_rejected_load_leaves_the_previous_index_intact(
    store: VectorStore, tmp_path: Path
) -> None:
    """A rejected artifact must not half-swap the live index onto a stale map."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload["count"] = 99
    _write_metadata(path, payload)

    live = VectorStore(dimension=2)
    live.add("keep", [1.0, 0.0])
    with pytest.raises(VectorArtifactVersionError):
        live.load(path)

    assert live.count() == 1
    assert live.index.ntotal == 1
    assert _ids(live.search([1.0, 0.0], limit=2)) == ["keep"]
    assert live.get_embedding("keep") == pytest.approx([1.0, 0.0])


def test_load_adopts_the_artifacts_own_dimension(engine: str, tmp_path: Path) -> None:
    """A valid artifact is adopted, not rejected against the constructor value."""
    source = VectorStore(dimension=3)
    source.add("c1", [1.0, 0.0, 0.0])
    path = tmp_path / "vectors"
    source.save(path)

    loaded = VectorStore(dimension=2)
    loaded.load(path)

    assert loaded.dimension == 3
    assert _ids(loaded.search([1.0, 0.0, 0.0], limit=1)) == ["c1"]


# --- Concurrency ----------------------------------------------------------


def test_concurrent_search_and_mutation_keep_the_index_consistent(
    store: VectorStore,
) -> None:
    """Barrier-synchronized readers never observe a partial mutation."""
    for i in range(20):
        store.add(f"seed{i}", [1.0, 0.0])

    searchers, mutators, rounds = 3, 3, 60
    barrier = threading.Barrier(searchers + mutators)
    errors: list[BaseException] = []

    def search_loop() -> None:
        barrier.wait(timeout=10)
        for _ in range(rounds):
            try:
                ids = _ids(store.search([1.0, 0.0], limit=5))
                assert len(ids) == len(set(ids)), ids
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
                return

    def mutate_loop(worker: int) -> None:
        barrier.wait(timeout=10)
        for i in range(rounds):
            try:
                store.upsert(f"w{worker}-{i % 5}", [0.0, 1.0])
                store.remove(f"w{worker}-{(i + 1) % 5}")
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
                return

    threads = [threading.Thread(target=search_loop) for _ in range(searchers)]
    threads += [
        threading.Thread(target=mutate_loop, args=(worker,))
        for worker in range(mutators)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert [t.name for t in threads if t.is_alive()] == []
    assert errors == []
    assert store.count() == store.index.ntotal


def test_concurrent_writers_have_a_bounded_deterministic_outcome(
    store: VectorStore,
) -> None:
    """Every writer's final ID survives exactly once, whatever the interleaving."""
    writers, per_writer = 4, 25
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def write_loop(worker: int) -> None:
        barrier.wait(timeout=10)
        for i in range(per_writer):
            try:
                store.add(f"w{worker}-{i}", [1.0, 0.0])
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
                return

    threads = [
        threading.Thread(target=write_loop, args=(worker,)) for worker in range(writers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert [t.name for t in threads if t.is_alive()] == []
    assert errors == []
    assert store.count() == writers * per_writer
    assert store.index.ntotal == store.count()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_seeded_operation_sequence_keeps_ids_and_native_rows_in_sync(
    store: VectorStore, seed: int
) -> None:
    """A reproducible add/upsert/remove sequence never drifts from its model."""
    rng = random.Random(seed)
    live: dict[str, list[float]] = {}

    for _ in range(150):
        chunk_id = f"c{rng.randrange(8)}"
        operation = rng.choice(["add", "upsert", "remove"])
        if operation == "remove":
            store.remove(chunk_id)
            live.pop(chunk_id, None)
        else:
            embedding = [rng.uniform(0.1, 1.0), rng.uniform(0.1, 1.0)]
            getattr(store, operation)(chunk_id, embedding)
            live[chunk_id] = embedding

        assert store.count() == len(live)
        assert store.index.ntotal == len(live)

    assert sorted(_ids(store.search([1.0, 0.0], limit=50))) == sorted(live)
    for chunk_id, embedding in live.items():
        stored = store.get_embedding(chunk_id)
        assert stored is not None
        assert stored == pytest.approx(embedding, abs=1e-6)


# --- Metadata validation helper ------------------------------------------


def test_validate_metadata_accepts_the_current_version() -> None:
    """The shared validator is the doctor-facing envelope check."""
    payload = VectorStore._validate_metadata(
        {"schema_version": VectorStore.SCHEMA_VERSION, "id_map": {}, "dimension": 2}
    )

    assert payload["schema_version"] == VectorStore.SCHEMA_VERSION


def test_validate_metadata_accepts_a_string_version() -> None:
    """A version serialized as text is normalized, not rejected."""
    payload = VectorStore._validate_metadata(
        {"schema_version": str(VectorStore.SCHEMA_VERSION), "id_map": {}}
    )

    assert payload["schema_version"] == VectorStore.SCHEMA_VERSION


def test_load_without_next_native_id_resumes_above_persisted_ids(
    store: VectorStore, tmp_path: Path
) -> None:
    """A missing cursor falls back to the highest persisted ID, avoiding reuse."""
    path = _saved_store(store, tmp_path / "vectors")
    payload = _read_metadata(path)
    payload.pop("next_native_id")
    _write_metadata(path, payload)

    loaded = VectorStore(dimension=2)
    loaded.load(path)
    loaded.add("c3", [0.7071, 0.7071])

    assert loaded.count() == 3
    assert loaded.index.ntotal == 3
    ids = _ids(loaded.search([1.0, 0.0], limit=5))
    assert sorted(ids) == ["c1", "c2", "c3"]


@pytest.mark.parametrize("bad_version", [1, 2, 999, "abc"])
def test_validate_metadata_rejects_unsupported_versions(bad_version: Any) -> None:
    """Legacy payloads are no longer migrated in memory; they fail closed."""
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        VectorStore._validate_metadata(
            {"schema_version": bad_version, "id_map": {}, "dimension": 2}
        )


def test_validate_metadata_rejects_a_missing_version() -> None:
    """A pre-versioning payload is unverifiable, so it is rejected."""
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        VectorStore._validate_metadata({"id_map": {}, "dimension": 2})


# --- Step 13: crash-safe artifact replacement ------------------------------


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise OSError("No space left on device")


def test_metadata_write_failure_preserves_the_previous_generation(
    store: VectorStore, engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed envelope replacement leaves the last saved generation loadable."""
    path = _saved_store(store, tmp_path / "vectors")
    previous = _read_metadata(path)

    store.add("c3", [0.5, 0.5])
    real_replace = atomic_write._replace
    monkeypatch.setattr(atomic_write, "_replace", _boom)
    with pytest.raises(OSError):
        store.save(path)
    # Restore only this seam: ``monkeypatch.undo()`` would also revert the
    # engine fixture's ``faiss = None`` patch and reload with the wrong engine.
    monkeypatch.setattr(atomic_write, "_replace", real_replace)

    assert _read_metadata(path) == previous
    reloaded = VectorStore(dimension=2)
    reloaded.load(path)
    assert sorted(reloaded.id_map.values()) == ["c1", "c2"]


def test_save_publishes_the_native_artifact_before_the_metadata_envelope(
    store: VectorStore, engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data first, metadata last: an envelope must never name absent rows."""
    path = tmp_path / "vectors"
    store.add("c1", [1.0, 0.0])
    published: list[str] = []

    real_replace = atomic_write._replace

    def spy(source: Path, destination: Path) -> None:
        published.append(Path(destination).name)
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write, "_replace", spy)
    store.save(path)

    assert published == [_native_artifact(path, engine).name, "vectors.json"]


def test_save_leaves_no_temporary_files(store: VectorStore, tmp_path: Path) -> None:
    """Publication cleans up its staging files."""
    path = _saved_store(store, tmp_path / "vectors")

    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(TEMP_SUFFIX)]
    assert leftovers == []


def test_load_rejects_a_truncated_metadata_envelope(
    store: VectorStore, tmp_path: Path
) -> None:
    """A half-written pre-Step-13 envelope fails closed with rebuild guidance."""
    path = _saved_store(store, tmp_path / "vectors")
    json_file = path.with_suffix(".json")
    text = json_file.read_text(encoding="utf-8")
    json_file.write_text(text[: len(text) // 2], encoding="utf-8")

    reloaded = VectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        reloaded.load(path)
