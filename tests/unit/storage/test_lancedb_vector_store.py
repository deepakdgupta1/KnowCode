"""Unit tests for the LanceDB vector store (Step 12).

Step 12 makes repository-derived chunk IDs *data* rather than executable filter
syntax, activates the Step 10 mutation/persistence contract for LanceDB, and
synchronizes its mutable write buffer with the durable table.

The reviewed defect: chunk IDs were interpolated straight into SQL-like
predicates (``where(f"id = '{chunk_id}'")``, ``delete(f"id = '{chunk_id}'")``),
so the ID ``x' OR true --`` widened a read to another row and deleted every row
in the table.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest

from knowcode.utils import atomic_write
from knowcode.utils.atomic_write import TEMP_SUFFIX
from knowcode.errors import (
    VectorArtifactVersionError,
    VectorContractError,
    VectorDimensionError,
)

try:
    from knowcode.storage.lancedb_vector_store import LanceDBVectorStore
except ImportError:  # pragma: no cover - exercised only without the extra
    LanceDBVectorStore = None  # type: ignore[assignment]


pytestmark = pytest.mark.skipif(
    LanceDBVectorStore is None, reason="lancedb not installed or module missing"
)


# Chunk IDs a repository can legitimately produce, plus the hostile forms the
# review reproduced. Every one of them must behave as an opaque exact key.
HOSTILE_IDS = [
    pytest.param("x' OR true --", id="or-true-comment"),
    pytest.param("x'); DROP TABLE vectors; --", id="statement-terminator"),
    pytest.param("a' OR id LIKE '%", id="like-wildcard"),
    pytest.param("'", id="bare-quote"),
    pytest.param("''", id="doubled-quote"),
    pytest.param('"', id="double-quote"),
    pytest.param("\\", id="backslash"),
    pytest.param("a\\'b", id="backslash-quote"),
    pytest.param("id = 'safe'", id="whole-predicate"),
    pytest.param("safe' OR 1=1 --", id="or-numeric"),
    pytest.param("a\nb\tc ", id="whitespace"),
    pytest.param("src/Ünïcode/файл.py::函数", id="unicode-path"),
    pytest.param("src/a-b_c.d/e[1].py::Cls.method", id="legal-fs-punctuation"),
    pytest.param("src/it's a file.py::fn", id="apostrophe-in-path"),
    pytest.param("a%b_c", id="sql-wildcards"),
]


@pytest.fixture
def make_store(tmp_path: Path):
    """Return ``build()`` producing a fresh store in its own directory."""
    counter = {"i": 0}

    def build(dimension: int = 2) -> "LanceDBVectorStore":
        counter["i"] += 1
        return LanceDBVectorStore(
            dimension=dimension, path=str(tmp_path / f"db{counter['i']}")
        )

    return build


@pytest.fixture
def vector_store(make_store):
    return make_store()


def durable_ids(store) -> list[str]:
    """Read chunk IDs straight out of the LanceDB table.

    Deliberately bypasses the store's in-memory bookkeeping: a widened delete
    would corrupt the *table* while leaving ``_live_keys`` looking correct, so
    the exactness gates have to read the durable rows themselves.
    """
    store.flush()
    table = store._table
    if table is None:
        return []
    total = int(table.count_rows())
    if total == 0:
        return []
    rows = table.search(None).select([store.ID_COLUMN]).limit(total).to_list()
    return sorted(str(row[store.ID_COLUMN]) for row in rows)


def assert_live(store, expected: list[str]) -> None:
    """Assert the durable table, the live count, and ``ids()`` all agree."""
    expected = sorted(expected)
    assert durable_ids(store) == expected
    assert store.ids() == expected
    assert store.count() == len(expected)


# --- Pre-existing behavior (kept green) --------------------------------------


def test_add_and_search(vector_store) -> None:
    vector_store.add("c1", [1.0, 0.0])
    vector_store.add("c2", [0.0, 1.0])

    results = vector_store.search([1.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0][0] == "c1"


def test_save_load_persistence(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("c1", [1.0, 0.0])
    store.add("c2", [0.0, 1.0])

    path = tmp_path / "saved_vectors"
    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    results = loaded.search([1.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0][0] == "c1"


def test_incremental_update(vector_store) -> None:
    vector_store.add("c1", [1.0, 0.0])
    results = vector_store.search([1.0, 0.0], limit=1)
    assert results[0][0] == "c1"

    vector_store.add("c2", [0.0, 1.0])
    results = vector_store.search([0.0, 1.0], limit=1)
    assert results[0][0] == "c2"


def test_recall_at_k(make_store) -> None:
    np.random.seed(42)
    dim = 16
    vectors = np.random.randn(100, dim).astype("float32")
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    store = make_store(dimension=dim)
    for i, vec in enumerate(vectors):
        store.add(f"c{i}", vec.tolist())

    results = store.search(vectors[0].tolist(), limit=5)

    assert results[0][0] == "c0"


def test_memory_profile(make_store) -> None:
    import tracemalloc

    store = make_store(dimension=128)
    vectors = np.random.randn(100, 128).astype("float32").tolist()

    tracemalloc.start()
    for i, vec in enumerate(vectors):
        store.add(f"c{i}", vec)
    store.search(vectors[0], limit=1)

    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 20 * 1024 * 1024  # 20MB upper bound for 100 vectors overhead


# --- Exact matching: chunk IDs are data, never filter syntax ------------------


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_id_removal_deletes_only_its_own_row(hostile: str, make_store) -> None:
    """``remove`` of a hostile ID leaves every non-target row intact."""
    store = make_store()
    store.add("safe-a", [0.0, 1.0])
    store.add("safe-b", [0.7071, 0.7071])
    store.add(hostile, [1.0, 0.0])
    assert_live(store, ["safe-a", "safe-b", hostile])

    store.remove(hostile)

    assert_live(store, ["safe-a", "safe-b"])
    assert store.get_embedding(hostile) is None


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_id_read_returns_only_its_own_vector(hostile: str, make_store) -> None:
    """``get_embedding`` of a hostile ID never widens to another row."""
    store = make_store()
    store.add("safe", [0.0, 1.0])
    store.add(hostile, [1.0, 0.0])

    embedding = store.get_embedding(hostile)

    assert embedding is not None
    assert embedding == pytest.approx([1.0, 0.0])


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_id_upsert_replaces_only_its_own_row(hostile: str, make_store) -> None:
    """``upsert`` inherits removal, so it must also stay exact."""
    store = make_store()
    store.add("safe", [0.0, 1.0])
    store.add(hostile, [1.0, 0.0])

    store.upsert(hostile, [0.7071, 0.7071])

    assert_live(store, ["safe", hostile])
    assert store.get_embedding("safe") == pytest.approx([0.0, 1.0])
    assert store.get_embedding(hostile) == pytest.approx([0.7071, 0.7071])


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_absent_hostile_id_removal_deletes_nothing(hostile: str, make_store) -> None:
    """Removing an ID that was never added is a no-op, however hostile."""
    store = make_store()
    store.add("safe-a", [0.0, 1.0])
    store.add("safe-b", [1.0, 0.0])

    store.remove(hostile)

    assert_live(store, ["safe-a", "safe-b"])


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_absent_hostile_id_read_returns_none(hostile: str, make_store) -> None:
    """Reading an absent hostile ID returns None, not another row's vector."""
    store = make_store()
    store.add("safe", [0.0, 1.0])

    assert store.get_embedding(hostile) is None


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_id_survives_save_and_load(
    hostile: str, tmp_path: Path, make_store
) -> None:
    """A hostile ID round-trips through persistence as exact-match data."""
    store = make_store()
    store.add("safe", [0.0, 1.0])
    store.add(hostile, [1.0, 0.0])
    path = tmp_path / "hostile_vectors"
    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)

    assert_live(loaded, ["safe", hostile])
    assert loaded.get_embedding(hostile) == pytest.approx([1.0, 0.0])
    loaded.remove(hostile)
    assert_live(loaded, ["safe"])


def test_hostile_id_removal_while_buffered_is_exact(make_store) -> None:
    """A hostile ID removed before its buffer flush cannot widen the delete."""
    store = make_store()
    store.add("safe", [0.0, 1.0])
    store.flush()
    store.add("x' OR true --", [1.0, 0.0])  # still buffered

    store.remove("x' OR true --")

    assert_live(store, ["safe"])


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_predicate_builder_emits_only_a_hex_literal(hostile: str) -> None:
    """The central predicate builder only ever emits a hex-keyed equality.

    Every LanceDB predicate this store issues is built here, so this is the one
    place where a repository ID could reach filter grammar. The builder names a
    digest column, so no input can carry a quote, operator, or comment into it.
    """
    predicate = LanceDBVectorStore._key_predicate(hostile)

    assert predicate.startswith(f"{LanceDBVectorStore.KEY_COLUMN} = '")
    assert predicate.endswith("'")
    body = predicate.split("'")[1]
    assert len(body) == 64
    assert all(char in "0123456789abcdef" for char in body)


def test_predicate_builder_rejects_a_non_digest_key() -> None:
    """A key that is not a digest is a programming error, not a predicate."""
    with pytest.raises(VectorContractError):
        LanceDBVectorStore._key_literal("x' OR true --")


def test_batched_delete_predicate_is_hex_only() -> None:
    """The batched flush predicate is built from digests too."""
    keys = [LanceDBVectorStore._digest(chunk_id) for chunk_id in ("a", "b")]

    predicate = LanceDBVectorStore._keys_predicate(keys)

    assert predicate == (
        f"{LanceDBVectorStore.KEY_COLUMN} IN ('{keys[0]}', '{keys[1]}')"
    )


def test_flush_deletes_more_rows_than_one_batch(make_store) -> None:
    """Pending deletes larger than one batch still remove exactly their rows."""
    store = make_store()
    total = LanceDBVectorStore.DELETE_BATCH_SIZE * 2 + 5
    for i in range(total):
        store.add(f"c{i}", [1.0, float(i)])
    store.flush()

    for i in range(total - 1):
        store.remove(f"c{i}")
    store.flush()

    assert_live(store, [f"c{total - 1}"])


# --- Step 10 contract: duplicates, count, dimension ---------------------------


def test_duplicate_add_replaces_the_single_live_row(vector_store) -> None:
    """``add`` is exact-ID add-or-replace, so one ID has one live row."""
    vector_store.add("a", [1.0, 0.0])
    vector_store.add("a", [0.0, 1.0])

    assert_live(vector_store, ["a"])
    assert vector_store.get_embedding("a") == pytest.approx([0.0, 1.0])


def test_duplicate_add_across_a_flush_replaces_the_durable_row(vector_store) -> None:
    """A replacement whose stale row is already durable leaves one row."""
    vector_store.add("a", [1.0, 0.0])
    vector_store.flush()
    vector_store.add("a", [0.0, 1.0])

    assert_live(vector_store, ["a"])
    assert vector_store.get_embedding("a") == pytest.approx([0.0, 1.0])


def test_duplicate_add_within_one_buffer_replaces_in_place(vector_store) -> None:
    """A replacement that never reached the table is still deduplicated."""
    vector_store.add("a", [1.0, 0.0])
    vector_store.add("a", [0.0, 1.0])
    vector_store.flush()

    assert_live(vector_store, ["a"])


def test_search_result_ids_are_unique_after_duplicate_add(vector_store) -> None:
    vector_store.add("a", [1.0, 0.0])
    vector_store.add("a", [0.0, 1.0])

    ids = [chunk_id for chunk_id, _ in vector_store.search([1.0, 0.0], limit=5)]

    assert ids == ["a"]


def test_count_tracks_live_rows_across_buffer_and_table(vector_store) -> None:
    assert_live(vector_store, [])
    vector_store.add("a", [1.0, 0.0])
    assert_live(vector_store, ["a"])
    vector_store.add("b", [0.0, 1.0])
    assert_live(vector_store, ["a", "b"])
    vector_store.remove("a")
    assert_live(vector_store, ["b"])


def test_dimension_is_validated_on_add_and_upsert(vector_store) -> None:
    with pytest.raises(VectorDimensionError):
        vector_store.add("bad", [1.0, 0.0, 0.0])
    with pytest.raises(VectorDimensionError):
        vector_store.upsert("bad", [1.0])
    assert vector_store.count() == 0


def test_clear_empties_buffer_and_table(vector_store) -> None:
    vector_store.add("a", [1.0, 0.0])
    vector_store.flush()
    vector_store.add("b", [0.0, 1.0])  # buffered

    vector_store.clear()

    assert_live(vector_store, [])
    assert vector_store.search([1.0, 0.0], limit=5) == []
    vector_store.add("c", [1.0, 0.0])
    assert_live(vector_store, ["c"])


# --- Buffer/table synchronization --------------------------------------------


def test_add_is_visible_to_a_later_read_without_an_explicit_flush(vector_store) -> None:
    """Reads flush first, so a returned ``add`` is always observable."""
    vector_store.add("a", [1.0, 0.0])

    assert vector_store.count() == 1
    assert vector_store.get_embedding("a") == pytest.approx([1.0, 0.0])
    assert vector_store.search([1.0, 0.0], limit=1)[0][0] == "a"


def test_remove_of_a_buffered_row_never_reaches_the_table(vector_store) -> None:
    """Add-then-remove before a flush leaves nothing durable to delete."""
    vector_store.add("a", [1.0, 0.0])
    vector_store.remove("a")
    vector_store.flush()

    assert_live(vector_store, [])
    assert vector_store.search([1.0, 0.0], limit=5) == []


def test_save_flushes_buffered_rows(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])  # buffered
    path = tmp_path / "flushed_vectors"

    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    assert_live(loaded, ["a"])


def test_load_discards_a_pending_buffer(tmp_path: Path, make_store) -> None:
    """Loading a generation replaces live state; it never merges a stale buffer."""
    source = make_store()
    source.add("saved", [1.0, 0.0])
    path = tmp_path / "loaded_vectors"
    source.save(path)

    target = make_store()
    target.add("stale", [0.0, 1.0])  # buffered, never saved
    target.load(path)

    assert_live(target, ["saved"])


# --- Metadata envelope, schema 2 ---------------------------------------------


def test_save_writes_schema_2_envelope(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])
    path = tmp_path / "vectors"

    store.save(path)

    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["backend"] == "lancedb"
    assert payload["dimension"] == 2
    assert payload["count"] == 2
    assert isinstance(payload["generation"], int)


def test_generation_advances_on_every_mutation(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    first = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))[
        "generation"
    ]

    store.remove("a")
    store.save(path)
    second = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))[
        "generation"
    ]

    assert second > first


def _write_envelope(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 2,
        "backend": "lancedb",
        "dimension": 2,
        "count": 1,
        "generation": 1,
    }
    payload.update(overrides)
    path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_rejects_legacy_schema_1_envelope(tmp_path: Path, make_store) -> None:
    """A v1 artifact predates exact-key columns, so it fails closed."""
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, schema_version=1)

    target = LanceDBVectorStore(dimension=2)
    with pytest.raises(VectorArtifactVersionError) as exc_info:
        target.load(path)

    assert "knowcode build" in str(exc_info.value)


def test_load_rejects_newer_schema(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, schema_version=99)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_rejects_missing_envelope_beside_an_existing_table(
    tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    path.with_suffix(".json").unlink()

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_rejects_a_foreign_backend_envelope(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, backend="faiss")

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_rejects_a_dimension_that_disagrees_with_the_table(
    tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, dimension=99)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=99).load(path)


def test_load_rejects_a_count_that_disagrees_with_the_table(
    tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, count=7)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_of_a_never_saved_path_is_an_empty_index(tmp_path: Path) -> None:
    """A fresh index directory loads as empty rather than failing closed."""
    store = LanceDBVectorStore(dimension=2, path=str(tmp_path / "fresh"))

    store.load(tmp_path / "never_saved")

    assert store.count() == 0


def test_load_of_a_connected_but_unsaved_index_is_empty(tmp_path: Path) -> None:
    """Connecting creates the directory; only a table means "saved".

    This is the service topology: ``create_vector_store`` points the store at
    ``<index>/vectors.lancedb`` (which ``lancedb.connect`` creates), and the
    indexer then calls ``load(<index>/vectors)`` on a directory that was never
    written. That must be an empty index, not a missing-envelope failure.
    """
    index_dir = tmp_path / "knowcode_index"
    index_dir.mkdir()
    store = LanceDBVectorStore(dimension=2, path=str(index_dir / "vectors.lancedb"))
    assert (index_dir / "vectors.lancedb").is_dir()

    store.load(index_dir / "vectors")

    assert store.count() == 0


def test_load_adopts_the_persisted_dimension(tmp_path: Path, make_store) -> None:
    store = make_store(dimension=3)
    store.add("a", [1.0, 0.0, 0.0])
    path = tmp_path / "vectors3"
    store.save(path)

    target = LanceDBVectorStore(dimension=3)
    target.load(path)

    assert target.dimension == 3
    assert target.get_embedding("a") == pytest.approx([1.0, 0.0, 0.0])


def test_load_rejects_non_object_metadata(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    path.with_suffix(".json").write_text("[]", encoding="utf-8")

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


@pytest.mark.parametrize(
    "schema_version", [None, "two", 2.5], ids=["missing", "non-numeric", "float"]
)
def test_load_rejects_an_unreadable_schema_version(
    schema_version: object, tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    if schema_version is None:
        del payload["schema_version"]
    else:
        payload["schema_version"] = schema_version
    path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_accepts_a_schema_version_written_as_a_string(
    tmp_path: Path, make_store
) -> None:
    """Envelope versions are normalized the same way FAISS normalizes them."""
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, schema_version=str(LanceDBVectorStore.SCHEMA_VERSION))

    target = LanceDBVectorStore(dimension=2)
    target.load(path)

    assert_live(target, ["a"])


def test_load_rejects_a_non_integer_dimension(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    _write_envelope(path, dimension="2")

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_rejects_an_envelope_whose_artifact_directory_is_gone(
    tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    shutil.rmtree(path.with_suffix(".lancedb"))

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_rejects_an_artifact_directory_without_the_table(
    tmp_path: Path, make_store
) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    shutil.rmtree(path.with_suffix(".lancedb"))
    path.with_suffix(".lancedb").mkdir()

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_load_without_a_generation_starts_at_zero(tmp_path: Path, make_store) -> None:
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    del payload["generation"]
    path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")

    target = LanceDBVectorStore(dimension=2)
    target.load(path)

    assert target._generation == 0


# --- Existing databases and legacy tables ------------------------------------


def test_constructing_on_an_existing_database_adopts_its_rows(
    tmp_path: Path, make_store
) -> None:
    """Reopening the same directory sees the rows it already holds."""
    directory = tmp_path / "reopened"
    first = LanceDBVectorStore(dimension=2, path=str(directory))
    first.add("a", [1.0, 0.0])
    first.add("b", [0.0, 1.0])
    first.flush()

    second = LanceDBVectorStore(dimension=2, path=str(directory))

    assert_live(second, ["a", "b"])
    second.remove("a")
    assert_live(second, ["b"])


def _write_legacy_table(directory: Path, rows: list[dict[str, object]]) -> None:
    """Write a pre-Step-12 table: an ``id``/``vector`` pair with no key column."""
    import lancedb
    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 2)),
        ]
    )
    lancedb.connect(str(directory)).create_table("vectors", data=rows, schema=schema)


def test_constructing_on_a_legacy_table_fails_closed(tmp_path: Path) -> None:
    """A table without the exact-key column cannot be addressed safely."""
    directory = tmp_path / "legacy.lancedb"
    _write_legacy_table(directory, [{"id": "a", "vector": [1.0, 0.0]}])

    with pytest.raises(VectorArtifactVersionError) as exc_info:
        LanceDBVectorStore(dimension=2, path=str(directory))

    assert "knowcode build" in str(exc_info.value)


def test_loading_a_legacy_table_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "vectors"
    _write_legacy_table(
        path.with_suffix(".lancedb"), [{"id": "a", "vector": [1.0, 0.0]}]
    )
    _write_envelope(path)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_loading_a_table_of_the_wrong_width_fails_closed(
    tmp_path: Path, make_store
) -> None:
    store = make_store(dimension=3)
    store.add("a", [1.0, 0.0, 0.0])
    path = tmp_path / "vectors3"
    store.save(path)
    _write_envelope(path, dimension=2)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


def test_loading_a_table_with_a_duplicated_chunk_id_fails_closed(
    tmp_path: Path, make_store
) -> None:
    """Two rows for one ID would make the live count a lie."""
    store = make_store()
    store.add("a", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    duplicate = {
        LanceDBVectorStore.KEY_COLUMN: LanceDBVectorStore._digest("a"),
        LanceDBVectorStore.ID_COLUMN: "a",
        LanceDBVectorStore.VECTOR_COLUMN: [0.0, 1.0],
    }
    import lancedb

    lancedb.connect(str(path.with_suffix(".lancedb"))).open_table("vectors").add(
        [duplicate]
    )
    _write_envelope(path, count=2)

    with pytest.raises(VectorArtifactVersionError):
        LanceDBVectorStore(dimension=2).load(path)


# --- In-memory databases ------------------------------------------------------


def test_in_memory_store_saves_and_reloads_its_rows(tmp_path: Path) -> None:
    """``save``/``load`` are symmetric even without a directory-backed source."""
    store = LanceDBVectorStore(dimension=2)
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])
    path = tmp_path / "memory_vectors"

    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    assert_live(loaded, ["a", "b"])
    assert loaded.get_embedding("a") == pytest.approx([1.0, 0.0])


def test_in_memory_store_saves_an_empty_index(tmp_path: Path) -> None:
    store = LanceDBVectorStore(dimension=2)
    path = tmp_path / "empty_vectors"

    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    assert_live(loaded, [])


def test_save_replaces_a_previously_exported_artifact(tmp_path: Path) -> None:
    store = LanceDBVectorStore(dimension=2)
    store.add("a", [1.0, 0.0])
    path = tmp_path / "memory_vectors"
    store.save(path)

    store.remove("a")
    store.add("b", [0.0, 1.0])
    store.save(path)

    loaded = LanceDBVectorStore(dimension=2)
    loaded.load(path)
    assert_live(loaded, ["b"])


def test_get_embedding_on_an_empty_store_returns_none() -> None:
    assert LanceDBVectorStore(dimension=2).get_embedding("a") is None


# --- Bookkeeping edge cases ---------------------------------------------------


def test_buffer_auto_flushes_at_its_limit(make_store) -> None:
    """Reaching the buffer limit drains it without an explicit flush."""
    store = make_store()
    for i in range(LanceDBVectorStore.BUFFER_LIMIT):
        store.add(f"c{i}", [1.0, float(i)])

    assert store._buffer == {}
    assert int(store._table.count_rows()) == LanceDBVectorStore.BUFFER_LIMIT


def test_a_digest_collision_is_reported_not_silently_widened(
    make_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two IDs sharing one key could not be addressed separately."""
    store = make_store()
    monkeypatch.setattr(
        LanceDBVectorStore, "_digest", classmethod(lambda cls, _: "a" * 64)
    )

    store.add("first", [1.0, 0.0])

    with pytest.raises(VectorContractError, match="share one exact-match key"):
        store.add("second", [0.0, 1.0])


def test_loading_a_table_whose_ids_collide_fails_closed(
    tmp_path: Path, make_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store()
    store.add("first", [1.0, 0.0])
    store.add("second", [0.0, 1.0])
    path = tmp_path / "vectors"
    store.save(path)

    monkeypatch.setattr(
        LanceDBVectorStore, "_digest", classmethod(lambda cls, _: "b" * 64)
    )
    with pytest.raises(VectorArtifactVersionError, match="share one exact-match key"):
        LanceDBVectorStore(dimension=2).load(path)


# --- Concurrency --------------------------------------------------------------


def test_concurrent_add_and_search_keep_the_store_consistent(make_store) -> None:
    """Barrier-synchronized writers/readers leave one coherent generation."""
    store = make_store()
    writers, readers = 4, 4
    per_writer = 10
    barrier = threading.Barrier(writers + readers)
    errors: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            barrier.wait(timeout=30)
            for i in range(per_writer):
                store.add(f"w{worker}-{i}", [1.0, float(i)])
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def read() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(per_writer):
                results = store.search([1.0, 0.0], limit=5)
                ids = [chunk_id for chunk_id, _ in results]
                assert len(ids) == len(set(ids))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    threads += [threading.Thread(target=read) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert not errors, errors
    expected = [f"w{w}-{i}" for w in range(writers) for i in range(per_writer)]
    assert_live(store, expected)


def test_concurrent_removal_and_search_never_expose_a_removed_id(make_store) -> None:
    """Removal is serialized with search: a removed ID never reappears."""
    store = make_store()
    for i in range(20):
        store.add(f"c{i}", [1.0, float(i)])
    store.flush()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    all_ids = {f"c{i}" for i in range(20)}

    def remover() -> None:
        try:
            barrier.wait(timeout=30)
            for i in range(20):
                store.remove(f"c{i}")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def searcher() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(20):
                ids = [chunk_id for chunk_id, _ in store.search([1.0, 0.0], limit=20)]
                # A search never sees a partially applied removal: every result
                # is a real, unique ID that has not been removed yet.
                assert len(ids) == len(set(ids))
                assert set(ids) <= all_ids
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=remover), threading.Thread(target=searcher)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert not errors, errors
    assert_live(store, [])
    assert store.search([1.0, 0.0], limit=5) == []


# --- Step 13: crash-safe metadata replacement ------------------------------


def test_metadata_write_failure_preserves_the_previous_generation(
    make_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed envelope replacement leaves the last saved generation loadable."""
    store = make_store()
    store.add("c1", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    previous = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))

    store.add("c2", [0.0, 1.0])

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("No space left on device")

    real_replace = atomic_write._replace
    monkeypatch.setattr(atomic_write, "_replace", boom)
    with pytest.raises(OSError):
        store.save(path)
    monkeypatch.setattr(atomic_write, "_replace", real_replace)

    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8")) == previous


def test_save_leaves_no_temporary_files(make_store, tmp_path: Path) -> None:
    """Publication cleans up its staging file."""
    store = make_store()
    store.add("c1", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(TEMP_SUFFIX)]
    assert leftovers == []


def test_load_rejects_a_truncated_metadata_envelope(make_store, tmp_path: Path) -> None:
    """A half-written pre-Step-13 envelope fails closed with rebuild guidance."""
    store = make_store()
    store.add("c1", [1.0, 0.0])
    path = tmp_path / "vectors"
    store.save(path)
    json_file = path.with_suffix(".json")
    text = json_file.read_text(encoding="utf-8")
    json_file.write_text(text[: len(text) // 2], encoding="utf-8")

    reloaded = make_store()
    with pytest.raises(VectorArtifactVersionError, match="knowcode build"):
        reloaded.load(path)
