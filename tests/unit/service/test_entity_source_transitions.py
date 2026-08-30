"""Entity-source mode transitions across builds (storage plan D3).

A generation's entity rows carry whatever the mode in force at *full-build*
time put there: text under `stored`, NULL under `disk`. Watch commits rewrite
chunks and vectors and copy ``knowledge.db`` unchanged (``generation_writer``:
"a file transaction rewrites chunks and vectors, never the graph"), so a mode
flip plus an incremental build leaves every entity row — text, NULL, and
digest — exactly as the last full build wrote it. These tests pin both
directions over that truth, including what each mode's reader serves for a
file edited after the last full build.
"""

import sqlite3
from pathlib import Path

from knowcode.config import AppConfig
from knowcode.service import KnowCodeService


def _source_tree(root: Path) -> Path:
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (src / "beta.py").write_text("def beta():\n    return 10\n", encoding="utf-8")
    return src


def _service(root: Path, entity_source: str) -> KnowCodeService:
    config = AppConfig.default()
    config.entity_source = entity_source
    return KnowCodeService(store_path=root, app_config=config)


def _eid(src: Path, file_name: str, fn: str) -> str:
    return f"{(src / file_name).resolve()}::{fn}"


def _current_generation(root: Path) -> Path:
    gens = root / "knowcode_index" / "generations"
    return sorted(gens.iterdir())[-1]


def _row_has_text(root: Path, file_name: str, fn: str) -> bool:
    """Whether one entity's row carries stored source in the live generation."""
    con = sqlite3.connect(
        f"file:{_current_generation(root) / 'knowledge.db'}?mode=ro", uri=True
    )
    try:
        row = con.execute(
            "SELECT source_code IS NOT NULL FROM entities WHERE entity_id LIKE ?",
            (f"%{file_name}::{fn}",),
        ).fetchone()
        return bool(row[0])
    finally:
        con.close()


# ----------------------------------------------------------------------
# stored -> disk
# ----------------------------------------------------------------------


def test_stored_to_disk_incremental_reads_verified_only_over_frozen_rows(
    tmp_path: Path,
) -> None:
    """A watched edit leaves the entity row stale; disk mode says so.

    The flip takes effect on the next read: the stored copy a `stored`-mode
    build left behind is ignored, so the edited file serves None (the entity
    digest predates the edit) while the untouched file still verifies.
    """
    src = _source_tree(tmp_path)
    stored = _service(tmp_path, "stored")
    stored.analyze(directory=src, output=tmp_path)
    stored.close()
    assert _row_has_text(tmp_path, "m.py", "alpha")

    # m.py changes and is watch-committed; the entity row is copied unchanged.
    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    disk = _service(tmp_path, "disk")
    disk.watch_writer().replace_file(src / "m.py")
    disk.flush()
    disk.close()
    assert _row_has_text(tmp_path, "m.py", "alpha"), (
        "a watch commit must not rewrite entity rows"
    )

    reader = _service(tmp_path, "disk")
    try:
        alpha = reader.get_entity_details(_eid(src, "m.py", "alpha"))
        beta = reader.get_entity_details(_eid(src, "beta.py", "beta"))

        # Edited after the last full build: the digest predates the edit, so
        # nothing the index can vouch for is served.
        assert alpha["source_code"] is None
        # Untouched since the last full build: resolves and verifies.
        assert "return 10" in (beta["source_code"] or "")
    finally:
        reader.close()

    # The same artifact under `stored` serves the stale copy with no signal.
    stored_reader = _service(tmp_path, "stored")
    try:
        alpha = stored_reader.get_entity_details(_eid(src, "m.py", "alpha"))
        assert "return 1" in (alpha["source_code"] or "")
    finally:
        stored_reader.close()


# ----------------------------------------------------------------------
# disk -> stored
# ----------------------------------------------------------------------


def test_disk_to_stored_incremental_resolves_null_rows_best_effort(
    tmp_path: Path,
) -> None:
    """`stored` over NULL rows has no copy to serve and resolves instead.

    The edited file has neither a stored copy nor a current digest: None.
    The untouched file resolves and verifies.
    """
    src = _source_tree(tmp_path)
    disk = _service(tmp_path, "disk")
    disk.analyze(directory=src, output=tmp_path)
    disk.close()
    assert not _row_has_text(tmp_path, "m.py", "alpha")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    stored = _service(tmp_path, "stored")
    stored.watch_writer().replace_file(src / "m.py")
    stored.flush()
    stored.close()
    assert not _row_has_text(tmp_path, "m.py", "alpha"), (
        "a watch commit must not rewrite entity rows"
    )

    reader = _service(tmp_path, "stored")
    try:
        alpha = reader.get_entity_details(_eid(src, "m.py", "alpha"))
        beta = reader.get_entity_details(_eid(src, "beta.py", "beta"))

        assert alpha["source_code"] is None
        assert "return 10" in (beta["source_code"] or "")
    finally:
        reader.close()


# ----------------------------------------------------------------------
# Full-rebuild flips, both directions
# ----------------------------------------------------------------------


def test_a_full_rebuild_after_either_flip_leaves_uniform_rows(
    tmp_path: Path,
) -> None:
    """Only a full build rewrites entity rows, so flips uniformize them."""
    src = _source_tree(tmp_path)

    disk = _service(tmp_path, "disk")
    disk.analyze(directory=src, output=tmp_path)
    disk.close()
    assert not _row_has_text(tmp_path, "beta.py", "beta")

    stored = _service(tmp_path, "stored")
    stored.analyze(directory=src, output=tmp_path)
    try:
        assert _row_has_text(tmp_path, "beta.py", "beta")
        beta = stored.get_entity_details(_eid(src, "beta.py", "beta"))
        assert "return 10" in (beta["source_code"] or "")
    finally:
        stored.close()

    disk_again = _service(tmp_path, "disk")
    disk_again.analyze(directory=src, output=tmp_path)
    try:
        assert not _row_has_text(tmp_path, "beta.py", "beta")
        beta = disk_again.get_entity_details(_eid(src, "beta.py", "beta"))
        # NULL rows over an unchanged file: resolution serves the same text.
        assert "return 10" in (beta["source_code"] or "")
    finally:
        disk_again.close()
