"""Unit tests for SQLite-backed knowledge store."""

import sqlite3
import threading
from pathlib import Path

import pytest

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.errors import RepositoryClosedError
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore


def _make_entity(entity_id: str, kind: EntityKind, name: str) -> Entity:
    return Entity(
        id=entity_id,
        kind=kind,
        name=name,
        qualified_name=name,
        location=Location("file.py", 1, 1),
        metadata={"content_hash": "dummy_hash"},
    )


def test_entity_round_trip(tmp_path: Path) -> None:
    """Test inserting and retrieving entities."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    store.add_entity(foo)

    # Retrieve by ID
    retrieved = store.get_entity("file.py::foo")
    assert retrieved is not None
    assert retrieved.id == foo.id
    assert retrieved.kind == foo.kind
    assert retrieved.name == foo.name

    # Search by pattern
    results = store.search("fo")
    assert len(results) == 1
    assert results[0].id == foo.id


def test_relationship_queries(tmp_path: Path) -> None:
    """Test querying relationships."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    bar = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    store.add_entity(foo)
    store.add_entity(bar)

    rel = Relationship(source_id=foo.id, target_id=bar.id, kind=RelationshipKind.CALLS)
    store.add_relationship(rel)

    # test get_callers / get_callees
    assert store.get_callers(bar.id) == [foo]
    assert store.get_callees(foo.id) == [bar]


def test_recursive_trace_calls(tmp_path: Path) -> None:
    """Test multi-hop traversal using CTE."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    a = _make_entity("file.py::a", EntityKind.FUNCTION, "a")
    b = _make_entity("file.py::b", EntityKind.FUNCTION, "b")
    c = _make_entity("file.py::c", EntityKind.FUNCTION, "c")
    store.add_entity(a)
    store.add_entity(b)
    store.add_entity(c)

    store.add_relationship(
        Relationship(source_id=a.id, target_id=b.id, kind=RelationshipKind.CALLS)
    )
    store.add_relationship(
        Relationship(source_id=b.id, target_id=c.id, kind=RelationshipKind.CALLS)
    )

    # Trace callees depth 1
    callees_1 = store.trace_calls(a.id, direction="callees", depth=1)
    assert len(callees_1) == 1
    assert callees_1[0]["entity_id"] == b.id
    assert callees_1[0]["call_depth"] == 1

    # Trace callees depth 2
    callees_2 = store.trace_calls(a.id, direction="callees", depth=2)
    assert len(callees_2) == 2
    ids = {c["entity_id"] for c in callees_2}
    assert ids == {b.id, c.id}


def test_get_impact(tmp_path: Path) -> None:
    """Test get_impact calculation."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    core = _make_entity("file.py::core", EntityKind.CLASS, "core")
    dep1 = _make_entity("file.py::dep1", EntityKind.FUNCTION, "dep1")
    dep2 = _make_entity("file.py::dep2", EntityKind.FUNCTION, "dep2")

    store.add_entity(core)
    store.add_entity(dep1)
    store.add_entity(dep2)

    store.add_relationship(
        Relationship(source_id=dep1.id, target_id=core.id, kind=RelationshipKind.CALLS)
    )
    store.add_relationship(
        Relationship(source_id=dep2.id, target_id=dep1.id, kind=RelationshipKind.CALLS)
    )

    impact = store.get_impact(core.id, max_depth=2)
    assert impact["entity_id"] == core.id
    assert len(impact["direct_dependents"]) == 1
    assert impact["direct_dependents"][0]["entity_id"] == dep1.id
    assert len(impact["transitive_dependents"]) == 1
    assert impact["transitive_dependents"][0]["entity_id"] == dep2.id


def test_json_migration(tmp_path: Path) -> None:
    """Test importing from JSON knowledge store."""
    json_path = tmp_path / "knowledge.json"
    legacy_store = KnowledgeStore()
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    bar = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    legacy_store.entities = {foo.id: foo, bar.id: bar}
    rel = Relationship(source_id=foo.id, target_id=bar.id, kind=RelationshipKind.CALLS)
    legacy_store.relationships = [rel]
    legacy_store.save(json_path)

    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore.from_json(json_path, db_path)

    retrieved = store.get_entity(foo.id)
    assert retrieved is not None
    assert retrieved.name == foo.name
    assert len(store.relationships) == 1


def test_concurrent_reads(tmp_path: Path) -> None:
    """Test WAL mode concurrent reads."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    store.add_entity(foo)

    def read_task() -> None:
        reader_store = SqliteKnowledgeStore(db_path)
        assert reader_store.get_entity(foo.id) is not None

    threads = [threading.Thread(target=read_task) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_sqlite_knowledge_store_helpers(tmp_path: Path) -> None:
    """Test various helper methods in SqliteKnowledgeStore."""
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path)

    # Create entities
    mod = _make_entity("file.py", EntityKind.MODULE, "file")
    cls = _make_entity("file.py::MyClass", EntityKind.CLASS, "MyClass")
    func = _make_entity("file.py::func", EntityKind.FUNCTION, "func")
    other = _make_entity("other.py", EntityKind.MODULE, "other")

    store.add_entity(mod)
    store.add_entity(cls)
    store.add_entity(func)
    store.add_entity(other)

    # Create relationships
    rel_contains = Relationship(
        source_id=mod.id, target_id=cls.id, kind=RelationshipKind.CONTAINS
    )
    rel_calls = Relationship(
        source_id=cls.id, target_id=func.id, kind=RelationshipKind.CALLS
    )
    rel_imports = Relationship(
        source_id=mod.id, target_id=other.id, kind=RelationshipKind.IMPORTS
    )

    store.add_relationship(rel_contains)
    store.add_relationship(rel_calls)
    store.add_relationship(rel_imports)

    # Test parent/children
    assert store.get_parent(cls.id).id == mod.id
    assert [c.id for c in store.get_children(mod.id)] == [cls.id]
    assert store.get_parent(mod.id) is None

    # Test imports
    assert store.get_imports(mod.id) == [other.id]

    # Test dependencies / dependents
    assert {d.id for d in store.get_dependencies(cls.id)} == {func.id}
    assert {d.id for d in store.get_dependencies(mod.id)} == {other.id}
    assert {d.id for d in store.get_dependents(func.id)} == {cls.id}
    assert {d.id for d in store.get_dependents(other.id)} == {mod.id}

    # Test outgoing / incoming relationships
    assert {r.target_id for r in store.get_outgoing_relationships(mod.id)} == {
        cls.id,
        other.id,
    }
    assert {r.source_id for r in store.get_incoming_relationships(cls.id)} == {mod.id}

    # Test entities_by_kind and list_by_kind
    assert {e.id for e in store.get_entities_by_kind(EntityKind.MODULE)} == {
        mod.id,
        other.id,
    }
    assert {e.id for e in store.list_by_kind("module")} == {mod.id, other.id}
    assert store.get_entities_by_kind("invalid_kind") == []

    # Test entities and relationships property
    assert len(store.entities) == 4
    assert len(store.relationships) == 3

    # Test invalid trace_calls direction
    with pytest.raises(ValueError, match="direction must be"):
        store.trace_calls(func.id, direction="invalid")

    # Test non-existent entity get_impact
    impact = store.get_impact("non_existent")
    assert impact["error"] == "Entity not found"

    # Test impact on class and module for type_score coverage
    impact_cls = store.get_impact(cls.id)
    assert impact_cls["risk_score"] > 0
    impact_mod = store.get_impact(mod.id)
    assert impact_mod["risk_score"] > 0

    # Test close
    store.close()


# ---------------------------------------------------------------------------
# Connection ownership, batch transactions, and aggregates (Step 09 — ADR 2)
# ---------------------------------------------------------------------------


def test_bulk_insert_never_leaks_uncommitted_state(tmp_path: Path) -> None:
    """A reader on the shared store must not see an uncommitted bulk_insert.

    The writer's transaction is held open via the ``_commit_bulk`` seam while a
    reader thread on the SAME instance checks for the in-flight entity. With
    thread-local reader connections, the reader sees only the committed WAL
    snapshot.
    """
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.add_entity(_make_entity("file.py::seed", EntityKind.FUNCTION, "seed"))

    observed: list[bool] = []
    barrier = threading.Barrier(2, timeout=5.0)
    original = store._commit_bulk

    def pausing_commit(conn, entities, relationships) -> None:
        original(conn, entities, relationships)  # insert within the open txn
        barrier.wait()  # release the reader to observe
        barrier.wait()  # hold until the reader has observed

    def reader() -> None:
        barrier.wait()  # wait until the writer has inserted
        observed.append(store.get_entity("file.py::inflight") is not None)
        barrier.wait()  # release the writer to commit

    store._commit_bulk = pausing_commit  # type: ignore[assignment]
    inflight = _make_entity("file.py::inflight", EntityKind.FUNCTION, "inflight")
    writer = threading.Thread(target=store.bulk_insert, args=([inflight], []))
    reader_t = threading.Thread(target=reader)
    try:
        writer.start()
        reader_t.start()
        writer.join(timeout=10)
        reader_t.join(timeout=10)
    finally:
        store._commit_bulk = original  # type: ignore[assignment]
        store.close()

    assert observed == [False], "reader observed an uncommitted entity"
    assert not writer.is_alive() and not reader_t.is_alive()


def test_bulk_insert_rolls_back_on_injected_failure(tmp_path: Path) -> None:
    """A failure inside bulk_insert leaves the prior committed state intact."""
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.add_entity(_make_entity("file.py::seed", EntityKind.FUNCTION, "seed"))

    original = store._commit_bulk

    def failing_commit(conn, entities, relationships) -> None:
        # Perform the inserts, then fail so the whole transaction rolls back.
        original(conn, entities, relationships)
        raise sqlite3.OperationalError("injected failure during bulk_insert")

    store._commit_bulk = failing_commit  # type: ignore[assignment]
    new_entity = _make_entity("file.py::new", EntityKind.FUNCTION, "new")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.bulk_insert([new_entity], [])
    finally:
        store._commit_bulk = original  # type: ignore[assignment]

    # The rollback left the prior committed state intact; reads use a reader
    # connection that observes the committed snapshot.
    assert store.get_entity("file.py::seed") is not None
    assert store.get_entity("file.py::new") is None
    store.close()


def test_count_by_kind(tmp_path: Path) -> None:
    """count_by_kind returns server-side GROUP BY counts, not materialized rows."""
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.bulk_insert(
        entities=[
            _make_entity("file.py::f1", EntityKind.FUNCTION, "f1"),
            _make_entity("file.py::f2", EntityKind.FUNCTION, "f2"),
            _make_entity("file.py::Cls", EntityKind.CLASS, "Cls"),
        ],
        relationships=[
            Relationship(
                source_id="file.py::Cls",
                target_id="file.py::f1",
                kind=RelationshipKind.CONTAINS,
            ),
            Relationship(
                source_id="file.py::Cls",
                target_id="file.py::f2",
                kind=RelationshipKind.CONTAINS,
            ),
        ],
    )
    try:
        counts = store.count_by_kind()
        assert counts["entities"] == {"function": 2, "class": 1}
        assert counts["relationships"] == {"contains": 2}
    finally:
        store.close()


def test_concurrent_readers_see_committed_snapshot(tmp_path: Path) -> None:
    """Multiple reader threads each see the committed snapshot."""
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.add_entity(_make_entity("file.py::foo", EntityKind.FUNCTION, "foo"))

    errors: list[str] = []
    seen: list[bool] = []

    def reader() -> None:
        try:
            for _ in range(5):
                seen.append(store.get_entity("file.py::foo") is not None)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    store.close()

    assert not errors
    assert all(seen)


def test_close_is_idempotent_and_drains_in_flight_reader(tmp_path: Path) -> None:
    """close() waits for an in-flight reader to release its lease."""
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.add_entity(_make_entity("file.py::x", EntityKind.FUNCTION, "x"))

    entered = threading.Event()
    proceed = threading.Event()

    def slow_reader() -> None:
        with store._read_lease() as conn:
            entered.set()
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()
            assert proceed.wait(timeout=5), "reader never released"

    reader_t = threading.Thread(target=slow_reader)
    reader_t.start()
    assert entered.wait(timeout=5)

    closer = threading.Thread(target=store.close)
    closer.start()
    closer.join(timeout=0.5)
    assert closer.is_alive(), "close did not wait for the reader to drain"

    proceed.set()
    closer.join(timeout=5)
    reader_t.join(timeout=5)
    assert not closer.is_alive() and not reader_t.is_alive()
    store.close()  # idempotent


def test_closed_store_raises(tmp_path: Path) -> None:
    """Operations after close() raise RepositoryClosedError."""
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    store.close()
    with pytest.raises(RepositoryClosedError):
        store.get_entity("anything")
    with pytest.raises(RepositoryClosedError):
        store.add_entity(_make_entity("file.py::y", EntityKind.FUNCTION, "y"))
    store.close()  # idempotent
