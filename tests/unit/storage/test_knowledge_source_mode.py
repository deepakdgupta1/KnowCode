"""The persisted entity source copy is a configuration choice (D3).

`entity_source: disk` writes `entities.source_code` as NULL — the text is
resolved from the working tree and verified at read time. `stored` (or the
store's default) keeps today's behavior. The column exists in both modes, so
the setting flips per build without a schema migration.
"""

from pathlib import Path

from knowcode.data_models import Entity, EntityKind, Location
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.utils.entity_identity import compute_entity_content_hash


def _entity() -> Entity:
    entity = Entity(
        id="/repo/mod.py::alpha",
        kind=EntityKind.FUNCTION,
        name="alpha",
        qualified_name="alpha",
        location=Location(file_path="/repo/mod.py", line_start=4, line_end=5),
        source_code="def alpha():\n    return 1\n",
    )
    entity.metadata["content_hash"] = compute_entity_content_hash(entity)
    return entity


def _stored_source(db: Path) -> str | None:
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT source_code FROM entities WHERE entity_id = ?",
            ("/repo/mod.py::alpha",),
        ).fetchone()[0]
    finally:
        con.close()


def test_the_default_store_persists_the_source_copy(tmp_path: Path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "k.db")
    try:
        store.add_entity(_entity())
    finally:
        store.close()

    assert _stored_source(tmp_path / "k.db") == "def alpha():\n    return 1\n"


def test_disk_mode_writes_null_but_keeps_the_digest(tmp_path: Path) -> None:
    entity = _entity()
    store = SqliteKnowledgeStore(tmp_path / "k.db", persist_entity_source=False)
    try:
        store.add_entity(entity)
    finally:
        store.close()

    assert _stored_source(tmp_path / "k.db") is None

    reloaded = SqliteKnowledgeStore(tmp_path / "k.db")
    try:
        hydrated = reloaded.get_entity(entity.id)
    finally:
        reloaded.close()
    # The digest survives: it is what a verified disk read is checked against.
    assert hydrated.source_code is None
    assert hydrated.metadata["content_hash"] == entity.metadata["content_hash"]


def test_bulk_insert_honours_the_mode_too(tmp_path: Path) -> None:
    """The build path writes through bulk_insert; it shares the seam."""
    entity = _entity()
    store = SqliteKnowledgeStore(tmp_path / "k.db", persist_entity_source=False)
    try:
        store.bulk_insert(entities=[entity], relationships=[])
    finally:
        store.close()

    assert _stored_source(tmp_path / "k.db") is None
