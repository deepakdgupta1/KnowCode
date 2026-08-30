"""The knowledge store anchors entity ids and edge endpoints at a root."""

import sqlite3
from pathlib import Path

import pytest

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore

ROOT = "/repo/root"
FOO = f"{ROOT}/src/mod.py::foo"
BAR = f"{ROOT}/src/mod.py::bar"
EXTERNAL = "external::requests::Session.get"
UNRESOLVED = f"unresolved::python::{ROOT}/src/mod.py::foo::mystery"


def _entity(entity_id: str, name: str) -> Entity:
    return Entity(
        id=entity_id,
        kind=EntityKind.FUNCTION,
        name=name,
        qualified_name=name,
        location=Location(f"{ROOT}/src/mod.py", 1, 2),
        metadata={"content_hash": "dummy"},
    )


@pytest.fixture()
def store(tmp_path: Path):
    knowledge = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    knowledge.set_repo_root(ROOT)
    knowledge.bulk_insert(
        entities=[_entity(FOO, "foo"), _entity(BAR, "bar")],
        relationships=[
            Relationship(source_id=FOO, target_id=BAR, kind=RelationshipKind.CALLS),
            Relationship(
                source_id=FOO, target_id=EXTERNAL, kind=RelationshipKind.IMPORTS
            ),
            Relationship(
                source_id=BAR, target_id=UNRESOLVED, kind=RelationshipKind.CALLS
            ),
        ],
    )
    yield knowledge
    knowledge.close()


def _column(db: Path, table: str, column: str) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(f"SELECT {column} FROM {table}")]
    finally:
        con.close()


def test_no_stored_id_carries_the_root(store, tmp_path: Path) -> None:
    db = tmp_path / "knowledge.db"

    stored = (
        _column(db, "entities", "entity_id")
        + _column(db, "entities", "file_path")
        + _column(db, "eid", "entity_id")
    )

    assert stored
    assert all(ROOT not in value for value in stored), stored


def test_the_endpoint_codebook_still_holds_every_endpoint(
    store, tmp_path: Path
) -> None:
    stored = set(_column(tmp_path / "knowledge.db", "eid", "entity_id"))

    assert len(stored) == 4
    assert EXTERNAL in stored


def test_an_entity_reads_back_with_its_absolute_id(store) -> None:
    entity = store.get_entity(FOO)

    assert entity is not None
    assert entity.id == FOO


def test_the_entities_mapping_is_keyed_by_absolute_id(store) -> None:
    assert set(store.entities) == {FOO, BAR}


def test_search_returns_absolute_ids(store) -> None:
    assert {e.id for e in store.search("foo")} == {FOO}


def test_edges_round_trip_all_three_endpoint_kinds(store) -> None:
    edges = {(r.source_id, r.kind.value, r.target_id) for r in store.relationships}

    assert edges == {
        (FOO, "calls", BAR),
        (FOO, "imports", EXTERNAL),
        (BAR, "calls", UNRESOLVED),
    }


def test_callers_and_callees_take_and_return_absolute_ids(store) -> None:
    assert [e.id for e in store.get_callees(FOO)] == [BAR]
    assert [e.id for e in store.get_callers(BAR)] == [FOO]


def test_outgoing_and_incoming_relationships_are_absolute(store) -> None:
    outgoing = store.get_outgoing_relationships(FOO)
    incoming = store.get_incoming_relationships(BAR)

    assert {r.target_id for r in outgoing} == {BAR, EXTERNAL}
    assert [r.source_id for r in incoming] == [FOO]


def test_imports_are_returned_as_absolute_endpoint_ids(store) -> None:
    assert store.get_imports(FOO) == [EXTERNAL]


def test_trace_calls_reports_absolute_ids(store) -> None:
    traced = store.trace_calls(FOO, direction="callees", depth=1)

    assert [row["entity_id"] for row in traced] == [BAR]


def test_without_a_root_ids_are_stored_and_returned_unchanged(
    tmp_path: Path,
) -> None:
    knowledge = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    try:
        knowledge.add_entity(_entity(FOO, "foo"))

        assert _column(tmp_path / "knowledge.db", "entities", "entity_id") == [FOO]
        assert knowledge.get_entity(FOO) is not None
    finally:
        knowledge.close()


def test_rebinding_a_populated_database_to_another_root_is_refused(store) -> None:
    with pytest.raises(ValueError, match="repository root"):
        store.set_repo_root("/somewhere/else")
