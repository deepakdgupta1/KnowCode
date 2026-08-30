"""An in-memory chunk database is never handed to a second repository (BL-7).

``:memory:`` is per-connection, so the writer/reader split routes an in-memory
database through a shared-cache URI. That URI used to be named by ``id(self)``,
and CPython reuses an object's address as soon as the object at it is freed. A
new repository could therefore reopen a dead one's database, complete with its
rows and the ``repo_root`` binding ADR 10 records, and the guard that refused
the rebind reported it as though the *caller* had moved the repository.

It read as a timing flake because it is allocation-dependent: it never
reproduces in a one-test run, hits a different test each time, and gets more
likely under load. It is not timing. Measured before the fix, 200 sequential
repositories shared **4** database names.

The load-bearing test is the sequential one. Two live repositories always had
distinct addresses, so a test that keeps both alive passes on exactly this
defect.

Deliberately not tested: that a *reused address* yields a different name.
Observing it needs the allocator to hand back the same address, which the fix
itself perturbs, so the test would sometimes prove nothing and sometimes fail
saying so. Adding a load-dependent test to close a load-dependent flake is not
a trade worth making. Sequential uniqueness is the same property without the
dependency: before the fix it measured 4 distinct names out of 50.
"""

import gc
from pathlib import Path

from knowcode.data_models import CodeChunk
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

DIMENSION = 4
ROUNDS = 50


def _chunk(chunk_id: str) -> CodeChunk:
    return CodeChunk(
        id=chunk_id,
        entity_id=f"/src/mod.py::{chunk_id}",
        content="body",
        tokens=["body"],
    )


def test_a_freed_repositorys_database_is_never_handed_to_the_next_one() -> None:
    names = []
    for _ in range(ROUNDS):
        repo = SqliteChunkRepository(":memory:", dimension=DIMENSION)
        names.append(repo._in_memory_uri)
        repo.close()
        del repo
        gc.collect()

    assert len(set(names)) == ROUNDS, (
        f"{ROUNDS - len(set(names))} of {ROUNDS} in-memory databases were reused"
    )


def test_two_live_repositories_do_not_share_a_database(tmp_path: Path) -> None:
    """The half that always held. Kept as the guard against over-correcting."""
    first = SqliteChunkRepository(":memory:", dimension=DIMENSION)
    second = SqliteChunkRepository(":memory:", dimension=DIMENSION)
    try:
        first.add(_chunk("only-in-first"))
        assert [chunk.id for chunk in first.get_all()] == ["only-in-first"]
        assert second.get_all() == []
    finally:
        first.close()
        second.close()


def test_a_file_backed_repository_takes_no_shared_cache_uri(tmp_path: Path) -> None:
    repo = SqliteChunkRepository(tmp_path / "chunks.db", dimension=DIMENSION)
    try:
        assert repo._in_memory is False
    finally:
        repo.close()
