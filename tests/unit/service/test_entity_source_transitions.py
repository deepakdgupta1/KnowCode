"""Entity-source mode transitions across builds (storage plan D3).

A generation's entity rows carry whatever the mode in force when they were
written put there: text under `stored`, NULL under `disk`. A full build writes
every row; a watch commit writes the rows of the file it touched, in the mode
in force *now* (BL-17 — it used to copy ``knowledge.db`` unchanged and write
none of them).

So flipping the mode and then watch committing leaves a deliberately mixed
artifact: the touched file's rows follow the new mode, every other file keeps
the old. Both halves read correctly — a NULL row resolves against its digest,
a text row serves its copy — and a full build makes the artifact uniform
again. These tests pin both directions over that, including what each mode's
reader serves for a file edited after the last full build.
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


def test_stored_to_disk_incremental_rewrites_the_touched_file_in_the_new_mode(
    tmp_path: Path,
) -> None:
    """A watched edit under `disk` replaces the `stored` build's text with NULL.

    The touched file's row is rewritten in the mode in force now, so its copy
    is dropped and its digest advances — the reader resolves the edit from
    disk and verifies it. The untouched file keeps the text the `stored` build
    wrote, which is the mixed artifact this flip is defined to produce.
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
    assert not _row_has_text(tmp_path, "m.py", "alpha"), (
        "the touched file's row must be rewritten in the mode in force now"
    )
    assert _row_has_text(tmp_path, "beta.py", "beta"), (
        "an untouched file must keep the row the last full build wrote"
    )

    reader = _service(tmp_path, "disk")
    try:
        alpha = reader.get_entity_details(_eid(src, "m.py", "alpha"))
        beta = reader.get_entity_details(_eid(src, "beta.py", "beta"))

        # The digest advanced with the edit, so the verified read serves it.
        assert "return 2" in (alpha["source_code"] or "")
        # Untouched since the last full build: resolves and verifies.
        assert "return 10" in (beta["source_code"] or "")
    finally:
        reader.close()

    # Reading the same artifact under `stored` finds no copy for the touched
    # file — the flip dropped it — and falls back to the same verified read.
    stored_reader = _service(tmp_path, "stored")
    try:
        alpha = stored_reader.get_entity_details(_eid(src, "m.py", "alpha"))
        assert "return 2" in (alpha["source_code"] or "")
    finally:
        stored_reader.close()


# ----------------------------------------------------------------------
# disk -> stored
# ----------------------------------------------------------------------


def test_disk_to_stored_incremental_resolves_null_rows_best_effort(
    tmp_path: Path,
) -> None:
    """A watched edit under `stored` gives the touched file a persisted copy.

    The `disk` build left every row NULL. The watch commit rewrites the touched
    file's row in the mode in force now, so that one row gains text while every
    other stays NULL and resolves from disk.
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
    assert _row_has_text(tmp_path, "m.py", "alpha"), (
        "the touched file's row must be rewritten in the mode in force now"
    )
    assert not _row_has_text(tmp_path, "beta.py", "beta"), (
        "an untouched file must keep the NULL row the last full build wrote"
    )

    reader = _service(tmp_path, "stored")
    try:
        alpha = reader.get_entity_details(_eid(src, "m.py", "alpha"))
        beta = reader.get_entity_details(_eid(src, "beta.py", "beta"))

        # Persisted by the watch commit.
        assert "return 2" in (alpha["source_code"] or "")
        # Still NULL, so it resolves from disk and verifies.
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
