"""Unit tests for knowledge store helpers and persistence."""

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


def test_persistence_round_trip(tmp_path) -> None:
    """Save/load should preserve entities, relationships, and metadata."""
    store = KnowledgeStore()
    store.metadata = {"stats": {"total": 1}}
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    store.entities = {foo.id: foo}
    store.relationships = [
        Relationship(
            source_id=foo.id,
            target_id="external::dep",
            kind=RelationshipKind.IMPORTS,
            metadata={"kind": "test"},
        )
    ]

    save_path = tmp_path / "knowledge.json"
    store.save(save_path)

    loaded = KnowledgeStore.load(save_path)
    assert loaded.metadata["stats"]["total"] == 1
    assert foo.id in loaded.entities
    assert loaded.relationships[0].metadata["kind"] == "test"
