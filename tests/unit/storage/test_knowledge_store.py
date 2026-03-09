"""Unit tests for knowledge store helpers and persistence."""

import json
from pathlib import Path

import pytest

from knowcode.data_models import Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.storage.knowledge_store import KnowledgeStore


def _make_entity(entity_id: str, kind: EntityKind, name: str) -> Entity:
    return Entity(
        id=entity_id,
        kind=kind,
        name=name,
        qualified_name=name,
        location=Location("file.py", 1, 1),
    )


def test_kind_filters_and_relationship_helpers() -> None:
    """KnowledgeStore should filter by kind and expose relationships."""
    store = KnowledgeStore()
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    bar = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    store.entities = {foo.id: foo, bar.id: bar}

    rel = Relationship(source_id=foo.id, target_id=bar.id, kind=RelationshipKind.CALLS)
    store.relationships = [rel]

    assert store.get_entities_by_kind("function") == [foo, bar]
    assert store.get_outgoing_relationships(foo.id) == [rel]
    assert store.get_incoming_relationships(bar.id) == [rel]


def test_persistence_round_trip(tmp_path) -> None:  # type: ignore
    """Save/load should preserve entities, relationships, and metadata."""
    store = KnowledgeStore()
    store.metadata = {"stats": {"total": 1}}
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    foo.metadata = {"is_async": False, "complexity": 5, "tags": ["core", "entry"]}
    store.entities = {foo.id: foo}
    store.relationships = [
        Relationship(
            source_id=foo.id,
            target_id="external::dep",
            kind=RelationshipKind.IMPORTS,
            metadata={"kind": "test", "weight": 0.75},
        )
    ]

    save_path = tmp_path / "knowledge.json"
    store.save(save_path)
    payload = json.loads(save_path.read_text(encoding="utf-8"))

    loaded = KnowledgeStore.load(save_path)
    assert payload["schema_version"] == KnowledgeStore.SCHEMA_VERSION
    assert loaded.metadata["stats"]["total"] == 1
    assert foo.id in loaded.entities
    assert loaded.entities[foo.id].metadata["is_async"] is False
    assert loaded.entities[foo.id].metadata["complexity"] == 5
    assert loaded.entities[foo.id].metadata["tags"] == ["core", "entry"]
    assert loaded.relationships[0].metadata["kind"] == "test"
    assert loaded.relationships[0].metadata["weight"] == 0.75


def test_load_migrates_legacy_store_without_schema_version(tmp_path: Path) -> None:
    """Legacy stores without schema_version should load via migration shim."""
    legacy = {
        "version": "1.0",
        "metadata": {"stats": {"total": 0}},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "legacy_knowledge.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    assert loaded.metadata["stats"]["total"] == 0


def test_load_migrates_legacy_schema_version_one(tmp_path: Path) -> None:
    """Explicit schema_version=1 payloads should migrate forward."""
    legacy = {
        "schema_version": 1,
        "metadata": {"stats": {"total": 2}},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "legacy_schema_one.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    assert loaded.metadata["stats"]["total"] == 2


def test_load_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    """Unknown schema versions should raise a clear compatibility error."""
    invalid = {
        "schema_version": 999,
        "metadata": {},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "bad_knowledge.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ValueError, match="schema version"):
        KnowledgeStore.load(path)


def test_load_backfills_entity_content_hash(tmp_path: Path) -> None:
    """Loading legacy entities should backfill missing content_hash metadata."""
    payload = {
        "schema_version": KnowledgeStore.SCHEMA_VERSION,
        "metadata": {},
        "entities": {
            "file.py::foo": {
                "id": "file.py::foo",
                "kind": "function",
                "name": "foo",
                "qualified_name": "foo",
                "location": {"file_path": "file.py", "line_start": 1, "line_end": 2},
                "source_code": "def foo():\n    return 1\n",
                "metadata": {},
            }
        },
        "relationships": [],
    }
    path = tmp_path / "content_hash_backfill.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    entity = loaded.entities["file.py::foo"]

    content_hash = entity.metadata.get("content_hash")
    assert isinstance(content_hash, str)
    assert len(content_hash) == 64
