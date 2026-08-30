"""The term index is a contentless FTS5 table (storage plan D2, BL-13).

`tokens_text` is no longer stored: every mutating repository path writes or
deletes its FTS row explicitly, in the same transaction as its chunk row. The
load-bearing tests here are the deletions — BL-13's point was that a broken
build still *finds* new chunks while stale rows go on answering queries, so a
test that only checks inserts passes on exactly the defect this change fixes.
"""

from pathlib import Path

import pytest

from knowcode.data_models import CodeChunk
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.utils.tokenizer import tokenize_code


@pytest.fixture()
def repo(tmp_path: Path):
    repository = SqliteChunkRepository(tmp_path / "chunks.db")
    yield repository
    repository.close()


def _chunk(file_path: str, name: str, content: str) -> CodeChunk:
    entity_id = f"{file_path}::{name}"
    return CodeChunk(
        id=f"{entity_id}::0",
        entity_id=entity_id,
        content=content,
        tokens=tokenize_code(content),
    )


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_the_chunks_table_no_longer_stores_tokens_text(repo) -> None:
    columns = {row[1] for row in repo._writer_conn.execute("PRAGMA table_info(chunks)")}
    assert "tokens_text" not in columns


def test_the_fts_table_is_contentless_with_delete_enabled(repo) -> None:
    """A contentless table without contentless_delete cannot delete a row."""
    fts_sql = repo._writer_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone()[0]
    assert "content=''" in fts_sql
    assert "contentless_delete=1" in fts_sql


def test_no_sync_triggers_remain(repo) -> None:
    triggers = [
        row[0]
        for row in repo._writer_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    ]
    assert triggers == []


# ---------------------------------------------------------------------------
# The BL-13 pin: deletions must leave the term index
# ---------------------------------------------------------------------------


class TestDeletedRowsStopMatching:
    """Remove, replace, and rewrite paths must retire the old terms."""

    def test_remove_by_file_stops_matching(self, repo) -> None:
        repo.add(_chunk("/src/keep.py", "kept", "def kept_symbol(): pass"))
        repo.add(_chunk("/src/gone.py", "gone", "def gone_symbol(): pass"))

        removed = repo.remove_by_file("/src/gone.py")

        assert removed == ["/src/gone.py::gone::0"]
        assert [c.id for c in repo.search_by_tokens(["gone_symbol"])] == []
        kept = [c.id for c in repo.search_by_tokens(["kept_symbol"])]
        assert kept == ["/src/keep.py::kept::0"]

    def test_replace_file_retires_the_old_terms(self, repo) -> None:
        repo.replace_file(
            "/src/mod.py",
            [_chunk("/src/mod.py", "fn", "def old_name(): pass")],
        )

        repo.replace_file(
            "/src/mod.py",
            [_chunk("/src/mod.py", "fn", "def new_name(): pass")],
        )

        assert [c.id for c in repo.search_by_tokens(["old_name"])] == []
        assert [c.id for c in repo.search_by_tokens(["new_name"])] == [
            "/src/mod.py::fn::0"
        ]

    def test_re_adding_a_chunk_id_leaves_no_stale_row(self, repo) -> None:
        """INSERT OR REPLACE hands the rewrite a fresh rowid; its old FTS row
        must not survive pointing at nothing."""
        repo.add(_chunk("/src/a.py", "fn", "def first_body(): pass"))
        repo.add(_chunk("/src/a.py", "fn", "def second_body(): pass"))

        assert [c.id for c in repo.search_by_tokens(["first_body"])] == []
        assert [c.id for c in repo.search_by_tokens(["second_body"])] == [
            "/src/a.py::fn::0"
        ]


# ---------------------------------------------------------------------------
# Search behaviour is unchanged (the plan's before/after gate)
# ---------------------------------------------------------------------------


def test_identifier_splitting_matches_as_before(repo) -> None:
    """snake_case, camelCase, and dotted paths retrieve through BM25 exactly
    as the external-content table did."""
    corpus = [
        _chunk("/src/users.py", "fetch", "def get_user_by_id(user_id): ..."),
        _chunk("/src/http.py", "client", "class HttpClient: ..."),
        _chunk("/src/paths.py", "join", "full = os.path.join(root, name)"),
        _chunk("/src/other.py", "unrelated", "def nothing_here(): pass"),
    ]
    repo.add_batch(corpus)

    # Compound identifiers stay searchable as one token...
    assert [c.id for c in repo.search_by_tokens(["getuserbyid"])] == [
        "/src/users.py::fetch::0"
    ]
    assert [c.id for c in repo.search_by_tokens(["httpclient"])] == [
        "/src/http.py::client::0"
    ]
    # ...and their subtokens too.
    assert {c.id for c in repo.search_by_tokens(["user"])} == {
        "/src/users.py::fetch::0"
    }
    # Dotted paths split into their components.
    assert [c.id for c in repo.search_by_tokens(["join"])] == ["/src/paths.py::join::0"]
    # Ranking still orders by bm25 across the corpus.
    ranked = [c.id for c in repo.search_by_tokens(["user", "join"])]
    assert set(ranked) == {"/src/users.py::fetch::0", "/src/paths.py::join::0"}


# ---------------------------------------------------------------------------
# Repair, compaction, and clearing
# ---------------------------------------------------------------------------


def test_rebuild_fts_reproduces_the_write_path_index(repo) -> None:
    chunks = [
        _chunk("/src/a.py", "one", "def first_function(): pass"),
        _chunk("/src/b.py", "two", "def second_function(): pass"),
    ]
    repo.add_batch(chunks)
    before = {c.id for c in repo.search_by_tokens(["function"])}

    # Wipe the term index the way corruption would.
    repo._writer_conn.execute("DELETE FROM chunks_fts")
    repo._writer_conn.commit()
    assert repo.search_by_tokens(["function"]) == []

    repo.rebuild_fts()

    assert {c.id for c in repo.search_by_tokens(["function"])} == before


def test_compact_preserves_the_term_index(repo) -> None:
    """Publication VACUUMs the staged database; the contentless index must
    survive it."""
    repo.add(_chunk("/src/a.py", "fn", "def survives_vacuum(): pass"))

    repo.compact()

    assert [c.id for c in repo.search_by_tokens(["survives_vacuum"])] == [
        "/src/a.py::fn::0"
    ]


def test_clear_empties_the_term_index(repo) -> None:
    repo.add(_chunk("/src/a.py", "fn", "def cleared_away(): pass"))

    repo.clear()

    assert repo.search_by_tokens(["cleared_away"]) == []
    assert repo.count() == 0


# ---------------------------------------------------------------------------
# The SQLite floor (BL-13: declare it rather than discover it)
# ---------------------------------------------------------------------------


def test_below_the_sqlite_floor_refuses_to_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SqliteChunkRepository, "MINIMUM_SQLITE_VERSION", (4, 0, 0))

    with pytest.raises(RuntimeError) as excinfo:
        SqliteChunkRepository(tmp_path / "chunks.db")

    message = str(excinfo.value)
    assert "4.0.0" in message
    assert "contentless_delete" in message
