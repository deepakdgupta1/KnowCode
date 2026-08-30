"""Exact search means the literal string, wildcards included (BL-15).

Both SQLite stores answer a substring query with a ``LIKE`` pattern built by
interpolating the caller's text. In ``LIKE`` an ``_`` matches any single
character and a ``%`` matches any run, and both are ordinary characters in
source code, so the query served was not the query asked. The in-memory
``KnowledgeStore.search`` is the contract these tests hold the SQLite one to:
a case-insensitive literal substring over name or qualified name.

The load-bearing assertions are the negatives. A test that only checks the
matching row passes on exactly the defect this fixes, because a wildcard
pattern still matches its own literal.
"""

from pathlib import Path

import pytest

from knowcode.data_models import CodeChunk, Entity, EntityKind, Location
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.utils.tokenizer import tokenize_code


@pytest.fixture()
def repo(tmp_path: Path):
    repository = SqliteChunkRepository(tmp_path / "chunks.db")
    yield repository
    repository.close()


def _chunk(chunk_id: str, content: str) -> CodeChunk:
    return CodeChunk(
        id=chunk_id,
        entity_id=f"/src/{chunk_id}.py::{chunk_id}",
        content=content,
        tokens=tokenize_code(content),
    )


def _entity(name: str) -> Entity:
    return Entity(
        id=f"/src/mod.py::{name}",
        kind=EntityKind.FUNCTION,
        name=name,
        qualified_name=f"mod.{name}",
        location=Location("/src/mod.py", 1, 1),
        metadata={"content_hash": "dummy_hash"},
    )


def _served(repo: SqliteChunkRepository, pattern: str) -> set[str]:
    return {c.id for c in repo.search_exact(pattern, limit=100)}


# ---------------------------------------------------------------------------
# chunks: search_exact
# ---------------------------------------------------------------------------


def test_an_underscore_does_not_match_a_different_character(repo) -> None:
    repo.add(_chunk("literal", "load vector_store now"))
    repo.add(_chunk("wildcard", "load vectorXstore now"))

    assert _served(repo, "vector_store") == {"literal"}


def test_a_percent_does_not_match_a_run(repo) -> None:
    repo.add(_chunk("literal", "coverage 100% today"))
    repo.add(_chunk("wildcard", "coverage 100 percent today"))

    assert _served(repo, "100% t") == {"literal"}


def test_a_backslash_matches_a_literal_backslash(repo) -> None:
    repo.add(_chunk("literal", r"open C:\tmp\out"))
    repo.add(_chunk("stripped", "open C:tmpout"))

    assert _served(repo, r"C:\tmp") == {"literal"}


def test_an_exact_query_still_serves_the_literal_it_names(repo) -> None:
    repo.add(_chunk("literal", "load vector_store now"))
    repo.add(_chunk("other", "load nothing now"))

    assert _served(repo, "vector_store") == {"literal"}
    assert _served(repo, "load") == {"literal", "other"}


def test_exact_search_is_case_insensitive_over_ascii(repo) -> None:
    repo.add(_chunk("upper", "load VECTOR_STORE now"))

    assert _served(repo, "vector_store") == {"upper"}


# ---------------------------------------------------------------------------
# entities: SqliteKnowledgeStore.search agrees with the in-memory contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["get_entity", "100%", r"a\b", "_"])
def test_entity_search_answers_what_the_in_memory_store_answers(
    tmp_path: Path, pattern: str
) -> None:
    names = ["get_entity", "getXentity", "at100%off", "at100_way_off", r"a\b", "ab"]

    memory = KnowledgeStore()
    sqlite_store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    for name in names:
        entity = _entity(name)
        memory.entities[entity.id] = entity
        sqlite_store.add_entity(entity)

    expected = {e.id for e in memory.search(pattern)}
    actual = {e.id for e in sqlite_store.search(pattern)}
    sqlite_store.close()

    assert actual == expected


def test_entity_search_still_finds_the_name_it_names(tmp_path: Path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    store.add_entity(_entity("get_entity"))
    store.add_entity(_entity("unrelated"))

    assert {e.name for e in store.search("get_entity")} == {"get_entity"}
    store.close()
