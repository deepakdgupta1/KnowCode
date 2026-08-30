"""A staged rewrite witnesses its own losslessness (BL-8).

Every number a generation manifest records about ``chunks.db`` and
``knowledge.db`` is read out of those files after the last step that rewrites
them, so a step that drops rows shrinks the artifact and the numbers together
and they agree. A generation missing 14% of its chunks published with
``validate_generation(verify_digests=True)`` returning no failures.

The fix is temporal, not structural. ``compact()`` reads a witness from its own
connection before the rewrite and compares after, so the comparison cannot be
satisfied by the damage.

Two choices carry these tests. The witness digests a sorted *list*, not a set,
because two identical edges are legal and a set-based digest cannot see one of
them go. And it covers which chunks carry an embedding, not just how many,
because that is the blind spot D1's durable-embedding guard shares.
"""

from pathlib import Path

import pytest

from knowcode.data_models import (
    CodeChunk,
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.errors import StagedRewriteError
from knowcode.storage.rewrite_witness import rows_preserved
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.utils.tokenizer import tokenize_code

DIMENSION = 4


def _chunk(name: str, embedding: list[float] | None = None) -> CodeChunk:
    entity_id = f"/src/mod.py::{name}"
    return CodeChunk(
        id=f"{entity_id}::0",
        entity_id=entity_id,
        content=f"def {name}(): return {name!r}",
        tokens=tokenize_code(name),
        embedding=embedding,
    )


def _entity(name: str) -> Entity:
    return Entity(
        id=f"/src/mod.py::{name}",
        kind=EntityKind.FUNCTION,
        name=name,
        qualified_name=f"mod.{name}",
        location=Location("/src/mod.py", 1, 2),
        metadata={"content_hash": "dummy_hash"},
    )


def _edge() -> Relationship:
    return Relationship(
        source_id="/src/mod.py::alpha",
        target_id="/src/mod.py::beta",
        kind=RelationshipKind.CALLS,
    )


class _LossyChunks(SqliteChunkRepository):
    """A chunk repository whose staged rewrite runs ``sql`` first."""

    sql = ""

    def _rewrite(self) -> None:
        self._writer_conn.execute(self.sql)
        # VACUUM refuses to run inside the transaction the probe just opened.
        self._writer_conn.commit()
        super()._rewrite()


class _LossyKnowledge(SqliteKnowledgeStore):
    """A knowledge store whose staged rewrite runs ``sql`` first."""

    sql = ""

    def _rewrite(self) -> None:
        self._writer_conn.execute(self.sql)
        # VACUUM refuses to run inside the transaction the probe just opened.
        self._writer_conn.commit()
        super()._rewrite()


# ---------------------------------------------------------------------------
# The bracket itself
# ---------------------------------------------------------------------------


def test_the_bracket_names_the_row_set_a_rewrite_changed() -> None:
    state = {"rows": ["a", "b"], "other": ["z"]}

    def witness() -> dict[str, str]:
        return {name: "|".join(sorted(rows)) for name, rows in state.items()}

    with pytest.raises(StagedRewriteError) as raised:
        with rows_preserved(witness, "chunks.db"):
            state["rows"] = ["a"]

    assert raised.value.changed == ["rows"]
    assert "chunks.db" in str(raised.value)


def test_the_bracket_permits_a_rewrite_that_changes_nothing() -> None:
    def witness() -> dict[str, str]:
        return {"rows": "a|b"}

    with rows_preserved(witness, "chunks.db"):
        pass


# ---------------------------------------------------------------------------
# chunks.db
# ---------------------------------------------------------------------------


def test_compact_fails_closed_when_the_rewrite_drops_a_chunk(tmp_path: Path) -> None:
    repo = _LossyChunks(tmp_path / "chunks.db", dimension=DIMENSION)
    repo.sql = "DELETE FROM chunks WHERE rowid = (SELECT MIN(rowid) FROM chunks)"
    repo.add(_chunk("alpha"))
    repo.add(_chunk("beta"))

    with pytest.raises(StagedRewriteError) as raised:
        repo.compact()
    assert "chunk_ids" in raised.value.changed
    repo.close()


def test_compact_fails_closed_when_the_rewrite_strips_an_embedding(
    tmp_path: Path,
) -> None:
    repo = _LossyChunks(tmp_path / "chunks.db", dimension=DIMENSION)
    repo.sql = (
        "UPDATE chunks SET embedding = NULL WHERE chunk_id = '/src/mod.py::alpha::0'"
    )
    repo.add(_chunk("alpha", [1.0, 0.0, 0.0, 0.0]))
    repo.add(_chunk("beta", [0.0, 1.0, 0.0, 0.0]))

    with pytest.raises(StagedRewriteError) as raised:
        repo.compact()
    assert raised.value.changed == ["embedded_chunk_ids"]
    repo.close()


def test_a_real_compact_preserves_every_chunk(tmp_path: Path) -> None:
    repo = SqliteChunkRepository(tmp_path / "chunks.db", dimension=DIMENSION)
    repo.add(_chunk("alpha", [1.0, 0.0, 0.0, 0.0]))
    repo.add(_chunk("beta", [0.0, 1.0, 0.0, 0.0]))

    repo.compact()

    rows = repo._writer_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert rows == 2
    repo.close()


# ---------------------------------------------------------------------------
# knowledge.db
# ---------------------------------------------------------------------------


def test_compact_fails_closed_when_the_rewrite_drops_an_entity(
    tmp_path: Path,
) -> None:
    store = _LossyKnowledge(tmp_path / "knowledge.db")
    store.sql = "DELETE FROM entities WHERE rowid = (SELECT MIN(rowid) FROM entities)"
    store.bulk_insert([_entity("alpha"), _entity("beta")], [])

    with pytest.raises(StagedRewriteError) as raised:
        store.compact()
    assert "entity_ids" in raised.value.changed
    store.close()


def test_compact_sees_one_of_two_identical_edges_go(tmp_path: Path) -> None:
    """A set-based digest cannot fail this; a multiset digest can."""
    store = _LossyKnowledge(tmp_path / "knowledge.db")
    store.sql = (
        "DELETE FROM relationships WHERE rowid = (SELECT MIN(rowid) FROM relationships)"
    )
    store.bulk_insert([_entity("alpha"), _entity("beta")], [_edge(), _edge()])
    held = store._writer_conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[
        0
    ]
    assert held == 2, "the probe needs two identical edges to delete one of"

    with pytest.raises(StagedRewriteError) as raised:
        store.compact()
    assert raised.value.changed == ["relationships"]
    store.close()


def test_compact_sees_an_edge_no_entity_row_explains_go(tmp_path: Path) -> None:
    """Resolving an edge through a table it may not appear in hides it (BL-17).

    Two thirds of real endpoints are ``external::`` or ``unresolved::`` ids
    with no ``entities`` row. An edge joined through ``entities`` is absent on
    both sides of the comparison, so the two sides agree and the loss passes.
    """
    store = _LossyKnowledge(tmp_path / "knowledge.db")
    store.sql = "DELETE FROM relationships WHERE source_id = 9001"
    store.bulk_insert([_entity("alpha"), _entity("beta")], [_edge()])
    store._writer_conn.execute(
        "INSERT INTO relationships (source_id, target_id, kind) VALUES (9001, 9002, 1)"
    )
    store._writer_conn.commit()
    orphans = store._writer_conn.execute(
        "SELECT COUNT(*) FROM relationships r LEFT JOIN eid s ON s.id = r.source_id"
        " WHERE s.id IS NULL"
    ).fetchone()[0]
    assert orphans == 1, "the probe needs an edge no codebook row explains"

    with pytest.raises(StagedRewriteError) as raised:
        store.compact()
    assert raised.value.changed == ["relationships"]
    store.close()


def test_compact_fails_closed_when_the_rewrite_drops_an_endpoint(
    tmp_path: Path,
) -> None:
    store = _LossyKnowledge(tmp_path / "knowledge.db")
    store.sql = "DELETE FROM eid WHERE rowid = (SELECT MIN(rowid) FROM eid)"
    store.bulk_insert([_entity("alpha"), _entity("beta")], [_edge()])

    with pytest.raises(StagedRewriteError) as raised:
        store.compact()
    assert raised.value.changed == ["edge_endpoints"]
    store.close()


def test_a_real_compact_preserves_the_whole_graph(tmp_path: Path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    store.bulk_insert([_entity("alpha"), _entity("beta")], [_edge()])

    store.compact()

    entities = store._writer_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    edges = store._writer_conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[
        0
    ]
    assert (entities, edges) == (2, 1)
    store.close()
