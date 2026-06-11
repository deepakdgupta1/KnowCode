"""Unit tests for SQLite-backed knowledge store."""

import json
import sqlite3
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
    legacy_store.entities = {foo.id: foo}
    legacy_store.save(json_path)
    
    db_path = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore.from_json(json_path, db_path)
    
    retrieved = store.get_entity(foo.id)
    assert retrieved is not None
    assert retrieved.name == foo.name


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
