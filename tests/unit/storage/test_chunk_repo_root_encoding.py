"""The chunk repository stores ids relative to a recorded repository root."""

import sqlite3
from pathlib import Path

import pytest

from knowcode.data_models import CodeChunk
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

ROOT = "/repo/root"


def _chunk(rel: str, name: str = "fn", index: int = 0) -> CodeChunk:
    entity_id = f"{ROOT}/{rel}::{name}"
    return CodeChunk(
        id=f"{entity_id}::{index}",
        entity_id=entity_id,
        content="body",
        tokens=["body"],
    )


@pytest.fixture()
def repo(tmp_path: Path):
    repository = SqliteChunkRepository(tmp_path / "chunks.db")
    repository.set_repo_root(ROOT)
    yield repository
    repository.close()


def _stored(db: Path, column: str) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(f"SELECT {column} FROM chunks")]
    finally:
        con.close()


def test_the_stored_columns_carry_no_root(repo, tmp_path: Path) -> None:
    repo.add(_chunk("src/mod.py"))

    for column in ("chunk_id", "entity_id", "file_path"):
        values = _stored(tmp_path / "chunks.db", column)
        assert values, column
        assert all(not v.startswith("/") for v in values), (column, values)
        assert all(ROOT not in v for v in values), (column, values)


def test_a_chunk_reads_back_with_its_absolute_ids(repo) -> None:
    chunk = _chunk("src/mod.py")

    repo.add(chunk)
    loaded = repo.get(chunk.id)

    assert loaded is not None
    assert loaded.id == chunk.id
    assert loaded.entity_id == chunk.entity_id


def test_lookup_by_absolute_entity_id_finds_the_chunk(repo) -> None:
    chunk = _chunk("src/mod.py")
    repo.add(chunk)

    found = repo.get_by_entity(chunk.entity_id)

    assert [c.id for c in found] == [chunk.id]


def test_get_all_returns_absolute_ids(repo) -> None:
    chunk = _chunk("src/mod.py")
    repo.add_batch([chunk])

    assert [c.id for c in repo.get_all()] == [chunk.id]


def test_file_paths_are_reported_absolute(repo) -> None:
    repo.add(_chunk("src/mod.py"))

    assert repo.get_all_file_paths() == {f"{ROOT}/src/mod.py"}


def test_remove_by_file_takes_an_absolute_path_and_returns_absolute_ids(
    repo,
) -> None:
    chunk = _chunk("src/mod.py")
    repo.add(chunk)

    removed = repo.remove_by_file(f"{ROOT}/src/mod.py")

    assert removed == [chunk.id]
    assert repo.count() == 0


def test_replace_file_round_trips_absolute_ids(repo) -> None:
    first = _chunk("src/mod.py", name="old")
    repo.add(first)
    second = _chunk("src/mod.py", name="new")

    receipt = repo.replace_file(f"{ROOT}/src/mod.py", [second])

    assert receipt.previous_chunk_ids == (first.id,)
    assert receipt.committed_chunk_ids == (second.id,)
    assert [c.id for c in repo.get_all()] == [second.id]


def test_faiss_index_resolves_back_to_an_absolute_chunk_id(repo) -> None:
    chunk = _chunk("src/mod.py")
    repo.add(chunk)

    idx = repo.get_faiss_idx(chunk.id)

    assert idx is not None
    assert repo.get_chunk_id_by_faiss_idx(idx) == chunk.id


def test_without_a_root_ids_are_stored_and_returned_unchanged(
    tmp_path: Path,
) -> None:
    repository = SqliteChunkRepository(tmp_path / "chunks.db")
    try:
        chunk = _chunk("src/mod.py")
        repository.add(chunk)

        assert _stored(tmp_path / "chunks.db", "chunk_id") == [chunk.id]
        assert repository.get(chunk.id) is not None
    finally:
        repository.close()


def test_rebinding_a_populated_database_to_another_root_is_refused(
    repo,
) -> None:
    repo.add(_chunk("src/mod.py"))

    with pytest.raises(ValueError, match="repository root"):
        repo.set_repo_root("/somewhere/else")
