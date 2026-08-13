"""Tests for SQLite chunk repository wiring in KnowCodeService."""

from pathlib import Path

from knowcode.config import AppConfig
from knowcode.data_models import Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.service import KnowCodeService
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
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


def test_get_indexer_wires_sqlite_by_default(tmp_path: Path) -> None:
    # Set up paths
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")

    # Initialize service
    service = KnowCodeService(store_path=tmp_path)

    # Retrieve indexer
    indexer = service.get_indexer()

    try:
        # Verify indexer chunk repo is SqliteChunkRepository
        assert isinstance(indexer.chunk_repo, SqliteChunkRepository)
        assert indexer.chunk_repo._db_path == tmp_path / "knowcode_index" / "chunks.db"
    finally:
        # Clean up
        indexer.chunk_repo.close()


def test_get_search_engine_wires_sqlite_by_default(tmp_path: Path) -> None:
    # Set up paths
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")

    # Initialize service
    service = KnowCodeService(store_path=tmp_path)

    # Retrieve search engine
    engine = service.get_search_engine()

    try:
        # Verify search engine chunk repo is SqliteChunkRepository
        assert isinstance(engine.chunk_repo, SqliteChunkRepository)
    finally:
        # Clean up
        engine.chunk_repo.close()


def test_analyze_routes_through_bulk_insert(tmp_path: Path, monkeypatch) -> None:
    """analyze() must persist the knowledge graph via bulk_insert (ADR 2)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    service = KnowCodeService(store_path=tmp_path, app_config=AppConfig.default())

    captured: dict = {}
    real_bulk = SqliteKnowledgeStore.bulk_insert

    def spy(self, entities, relationships) -> None:
        captured["entities"] = list(entities)
        captured["relationships"] = list(relationships)
        return real_bulk(self, entities, relationships)

    monkeypatch.setattr(SqliteKnowledgeStore, "bulk_insert", spy)

    stats = service.analyze(directory=src, output=tmp_path)
    assert stats["published"] is True

    # The store is re-opened from the published generation, not from the
    # staging directory it was written into.
    store = service.store
    assert isinstance(store, SqliteKnowledgeStore)
    try:
        # bulk_insert was invoked with the parsed entities, not a manual BEGIN.
        assert "entities" in captured
        assert len(captured["entities"]) >= 1
        # A parsed module -> function containment edge is passed as a relationship.
        assert len(captured["relationships"]) >= 1
        # The store was actually populated through bulk_insert.
        assert store.count_by_kind()["entities"]  # at least one kind
    finally:
        store.close()


def test_stats_uses_count_by_kind_without_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    """get_stats() must use count_by_kind and never materialize entities.

    SqliteKnowledgeStore defines an ``entities`` property, so the old
    ``hasattr(self.store, "entities")`` branch materialized every row just to
    count them. The capability dispatch must avoid that O(n) hydration.
    """
    store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
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
        ],
    )

    service = KnowCodeService(store_path=tmp_path, app_config=AppConfig.default())
    service._store = store
    service._indexer = None

    # Spy on the entities property to prove it is NOT accessed.
    materialized: list[bool] = []
    original_entities = SqliteKnowledgeStore.entities

    def spy_entities(self):  # type: ignore[no-untyped-def]
        materialized.append(True)
        return original_entities.fget(self)

    monkeypatch.setattr(SqliteKnowledgeStore, "entities", property(spy_entities))

    stats = service.get_stats()

    try:
        assert materialized == [], "get_stats materialized entities"
        assert stats["total_entities"] == 3
        assert stats["entities_by_kind"] == {"function": 2, "class": 1}
        assert stats["total_relationships"] == 1
        assert stats["relationships_by_type"] == {"contains": 1}
    finally:
        store.close()
