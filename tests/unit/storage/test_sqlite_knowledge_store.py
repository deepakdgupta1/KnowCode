"""Unit tests for SQLite-backed knowledge store."""

import threading
from pathlib import Path

import pytest

from knowcode.data_models import Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore


def _make_entity(entity_id: str, kind: EntityKind, name: str) -> Entity:
    return Entity(
        id=entity_id,
        kind=kind,
        name=name,
        qualified_name=name,
        location=Location("file.py", 1, 1),
        metadata={"content_hash": "dummy_hash"}
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
    
    store.add_relationship(Relationship(source_id=a.id, target_id=b.id, kind=RelationshipKind.CALLS))
    store.add_relationship(Relationship(source_id=b.id, target_id=c.id, kind=RelationshipKind.CALLS))
    
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
    
    store.add_relationship(Relationship(source_id=dep1.id, target_id=core.id, kind=RelationshipKind.CALLS))
    store.add_relationship(Relationship(source_id=dep2.id, target_id=dep1.id, kind=RelationshipKind.CALLS))
    
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
    rel_contains = Relationship(source_id=mod.id, target_id=cls.id, kind=RelationshipKind.CONTAINS)
    rel_calls = Relationship(source_id=cls.id, target_id=func.id, kind=RelationshipKind.CALLS)
    rel_imports = Relationship(source_id=mod.id, target_id=other.id, kind=RelationshipKind.IMPORTS)
    
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
    assert {r.target_id for r in store.get_outgoing_relationships(mod.id)} == {cls.id, other.id}
    assert {r.source_id for r in store.get_incoming_relationships(cls.id)} == {mod.id}
    
    # Test entities_by_kind and list_by_kind
    assert {e.id for e in store.get_entities_by_kind(EntityKind.MODULE)} == {mod.id, other.id}
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

