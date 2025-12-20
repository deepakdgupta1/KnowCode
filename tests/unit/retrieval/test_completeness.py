"""Unit tests for dependency expansion."""

from knowcode.data_models import CodeChunk, Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.retrieval.completeness import expand_dependencies
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.knowledge_store import KnowledgeStore


def test_expand_dependencies_dedupes() -> None:
    """Dependency expansion should include each chunk once."""
    repo = InMemoryChunkRepository()
    chunk_a = CodeChunk(id="c1", entity_id="e1", content="A", tokens=["a"])
    chunk_b = CodeChunk(id="c2", entity_id="e2", content="B", tokens=["b"])
    repo.add(chunk_a)
    repo.add(chunk_b)

    store = KnowledgeStore()
    entity_a = Entity(
        id="e1",
        kind=EntityKind.FUNCTION,
        name="a",
        qualified_name="a",
        location=Location("file.py", 1, 1),
    )
    entity_b = Entity(
        id="e2",
        kind=EntityKind.FUNCTION,
        name="b",
        qualified_name="b",
        location=Location("file.py", 2, 2),
    )
    store.entities = {entity_a.id: entity_a, entity_b.id: entity_b}
    store.relationships = [
        Relationship(
            source_id=entity_a.id,
            target_id=entity_b.id,
            kind=RelationshipKind.CALLS,
        )
    ]

    expanded = expand_dependencies(chunk_a, repo, store, max_depth=1)
    ids = [c.id for c in expanded]

    assert ids.count("c1") == 1
    assert ids.count("c2") == 1
