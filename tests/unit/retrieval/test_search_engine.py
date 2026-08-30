"""Unit tests for search engine dependency expansion."""

from knowcode.data_models import (
    CodeChunk,
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.knowledge_store import KnowledgeStore


class DummyEmbeddingProvider:
    def embed_single(self, _text):  # type: ignore
        return [0.0]


class StubHybridIndex:
    def __init__(self, results) -> None:  # type: ignore
        self._results = results

    def search(self, _query, _embedding, limit=10):  # type: ignore
        return self._results[:limit]


def test_search_engine_expands_dependencies() -> None:
    """Search should expand callees into the result set."""
    repo = SqliteChunkRepository(":memory:")
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
    engine = SearchEngine(repo, DummyEmbeddingProvider(), hybrid, store)  # type: ignore

    results = engine.search("a", limit=1, expand_deps=True)
    ids = {c.id for c in results}

    assert {"c1", "c2"} <= ids


def _chunk(name: str) -> CodeChunk:
    return CodeChunk(id=f"c_{name}", entity_id=f"f.py::{name}", content=f"body {name}")


def _entity(name: str) -> Entity:
    return Entity(
        id=f"f.py::{name}",
        kind=EntityKind.FUNCTION,
        name=name,
        qualified_name=name,
        location=Location("f.py", 1, 1),
    )


def _engine(ranked, calls):  # type: ignore[no-untyped-def]
    """Build an engine over a real repo and store, with a stubbed index.

    ``ranked`` is [(name, score)] the hybrid index returns; ``calls`` is
    [(caller, callee)] CALLS edges. Every name mentioned in either gets a
    chunk and an entity, so expansion has something real to resolve.
    """
    names = {n for n, _ in ranked} | {n for pair in calls for n in pair}
    repo = SqliteChunkRepository(":memory:")
    chunks = {n: _chunk(n) for n in names}
    for chunk in chunks.values():
        repo.add(chunk)

    store = KnowledgeStore()
    store.entities = {e.id: e for e in (_entity(n) for n in names)}
    store.relationships = [
        Relationship(
            source_id=f"f.py::{caller}",
            target_id=f"f.py::{callee}",
            kind=RelationshipKind.CALLS,
        )
        for caller, callee in calls
    ]

    hybrid = StubHybridIndex([(chunks[n], s) for n, s in ranked])
    return SearchEngine(
        repo,
        DummyEmbeddingProvider(),  # type: ignore[arg-type]
        hybrid,  # type: ignore[arg-type]
        store,
        use_voyageai_reranking=False,
    )


def test_a_ranked_hit_that_is_also_a_callee_stays_retrieval_evidence() -> None:
    """A hit on a call chain keeps its own score and its "retrieved" label.

    BL-18: expansion emitted ``[chunk] + callees`` into one shared ``seen_ids``
    set, so beta and gamma -- ranked in their own right -- were already in it
    as alpha's and beta's callees by the time their own turn came, and kept the
    ``score=0.0, source="dependency"`` label they were given as callees. The
    orchestrator selects on ``source == "retrieved"``, so a three-entity
    request over a call chain synthesized one.
    """
    engine = _engine(
        ranked=[("alpha", 0.9), ("beta", 0.85), ("gamma", 0.8)],
        calls=[("alpha", "beta"), ("beta", "gamma")],
    )

    scored = engine.search_scored("body", limit=3, expand_deps=True)

    retrieved = {s.chunk.entity_id: s.score for s in scored if s.source == "retrieved"}
    assert set(retrieved) == {"f.py::alpha", "f.py::beta", "f.py::gamma"}
    assert all(score > 0.0 for score in retrieved.values())


def test_a_callee_that_was_never_ranked_is_still_only_a_dependency() -> None:
    """The other half of BL-18: expansion must not promote what retrieval missed.

    Guards the lazy fix -- labelling everything "retrieved" would satisfy the
    test above while destroying the distinction the orchestrator selects on.
    """
    engine = _engine(ranked=[("alpha", 0.9)], calls=[("alpha", "delta")])

    scored = engine.search_scored("body", limit=3, expand_deps=True)

    by_entity = {s.chunk.entity_id: s for s in scored}
    assert by_entity["f.py::alpha"].source == "retrieved"
    assert by_entity["f.py::delta"].source == "dependency"
    assert by_entity["f.py::delta"].score == 0.0
