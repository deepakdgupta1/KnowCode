"""Unit tests for search engine dependency expansion."""

from knowcode.data_models import CodeChunk, Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.knowledge_store import KnowledgeStore


class DummyEmbeddingProvider:
    def embed_single(self, _text):
        return [0.0]


class StubHybridIndex:
    def __init__(self, results):
        self._results = results

    def search(self, _query, _embedding, limit=10):
        return self._results[:limit]


def test_search_engine_expands_dependencies() -> None:
    """Search should expand callees into the result set."""
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

    hybrid = StubHybridIndex([(chunk_a, 1.0)])
    engine = SearchEngine(repo, DummyEmbeddingProvider(), hybrid, store)

    results = engine.search("a", limit=1, expand_deps=True)
    ids = {c.id for c in results}

    assert {"c1", "c2"} <= ids
