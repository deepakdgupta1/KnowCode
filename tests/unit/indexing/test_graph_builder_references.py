"""Unit tests for graph builder reference resolution."""

from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.data_models import Entity, EntityKind, Location, Relationship, RelationshipKind


def test_reference_resolution() -> None:
    """ref:: targets should resolve to known entities when possible."""
    builder = GraphBuilder()
    entity = Entity(
        id="file.py::Foo",
        kind=EntityKind.CLASS,
        name="Foo",
        qualified_name="Foo",
        location=Location("file.py", 1, 5),
    )
    builder.entities = {entity.id: entity}
    builder.relationships = [
        Relationship(
            source_id="file.py::Caller",
            target_id="ref::Foo",
            kind=RelationshipKind.REFERENCES,
        )
    ]

    builder._resolve_references()

    assert builder.relationships[0].target_id == entity.id
