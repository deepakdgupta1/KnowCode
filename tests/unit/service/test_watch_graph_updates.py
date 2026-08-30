"""A watch commit advances the entity graph for the file it rewrites (BL-17).

`StagedGenerationWriter` used to copy `knowledge.db` unchanged — "a file
transaction rewrites chunks and vectors, never the graph" — so entity rows,
their digests, and their relationships described the last full build no matter
how many watched edits followed. Under `entity_source: stored` that served the
pre-edit copy as current; under the `disk` default it fails closed to None,
which is honest but leaves the file's source unavailable until a rebuild.

These pin the graph half of a file transaction: the edited file's own entities
are re-derived, everything else is left exactly as it was.
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


def _service(root: Path, entity_source: str = "disk") -> KnowCodeService:
    config = AppConfig.default()
    config.entity_source = entity_source
    return KnowCodeService(store_path=root, app_config=config)


def _eid(src: Path, file_name: str, fn: str) -> str:
    return f"{(src / file_name).resolve()}::{fn}"


def _current_generation(root: Path) -> Path:
    return sorted((root / "knowcode_index" / "generations").iterdir())[-1]


def _entity_names(root: Path, file_name: str) -> set[str]:
    """Every entity name the live generation holds for one file."""
    con = sqlite3.connect(
        f"file:{_current_generation(root) / 'knowledge.db'}?mode=ro", uri=True
    )
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM entities WHERE entity_id LIKE ?",
                (f"%{file_name}::%",),
            )
        }
    finally:
        con.close()


def _watch_commit(root: Path, path: Path) -> None:
    service = _service(root)
    try:
        service.watch_writer().replace_file(path)
        service.flush()
    finally:
        service.close()


def test_a_watch_commit_advances_the_edited_files_entity_rows(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    builder = _service(tmp_path)
    builder.analyze(directory=src, output=tmp_path)
    builder.close()

    (src / "m.py").write_text(
        "def alpha():\n    return 2\n\n\ndef gamma():\n    return 3\n",
        encoding="utf-8",
    )
    _watch_commit(tmp_path, src / "m.py")

    reader = _service(tmp_path)
    try:
        # The row's digest advanced with the edit, so the verified read serves.
        alpha = reader.get_entity_details(_eid(src, "m.py", "alpha"))
        assert alpha is not None
        assert "return 2" in (alpha["source_code"] or "")
        # A symbol the edit introduced is in the graph, not only in the chunks.
        assert "gamma" in _entity_names(tmp_path, "m.py")
    finally:
        reader.close()


def _edges_from(root: Path, file_name: str) -> int:
    """How many edges leave entities defined in one file."""
    con = sqlite3.connect(
        f"file:{_current_generation(root) / 'knowledge.db'}?mode=ro", uri=True
    )
    try:
        return con.execute(
            """
            SELECT COUNT(*) FROM relationships WHERE source_id IN (
                SELECT id FROM eid WHERE entity_id IN (
                    SELECT entity_id FROM entities WHERE entity_id LIKE ?
                )
            )
            """,
            (f"%{file_name}::%",),
        ).fetchone()[0]
    finally:
        con.close()


def test_a_watch_commit_leaves_other_files_entities_untouched(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    builder = _service(tmp_path)
    builder.analyze(directory=src, output=tmp_path)
    builder.close()
    before_names = _entity_names(tmp_path, "beta.py")
    before_edges = _edges_from(tmp_path, "beta.py")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    _watch_commit(tmp_path, src / "m.py")

    assert _entity_names(tmp_path, "beta.py") == before_names
    assert _edges_from(tmp_path, "beta.py") == before_edges

    reader = _service(tmp_path)
    try:
        beta = reader.get_entity_details(_eid(src, "beta.py", "beta"))
        assert "return 10" in (beta["source_code"] or "")
    finally:
        reader.close()


def test_a_watch_delete_removes_the_files_entities_from_the_graph(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    builder = _service(tmp_path)
    builder.analyze(directory=src, output=tmp_path)
    builder.close()
    assert "alpha" in _entity_names(tmp_path, "m.py")

    (src / "m.py").unlink()
    service = _service(tmp_path)
    try:
        service.watch_writer().delete_file(src / "m.py")
        service.flush()
    finally:
        service.close()

    assert _entity_names(tmp_path, "m.py") == set()
    assert _edges_from(tmp_path, "m.py") == 0
    # The file that was not touched is still whole.
    assert "beta" in _entity_names(tmp_path, "beta.py")


def test_a_watch_commit_reports_the_edge_count_it_actually_holds(
    tmp_path: Path,
) -> None:
    """The manifest counts the staged artifact, not the base it was seeded from."""
    import json

    src = _source_tree(tmp_path)
    builder = _service(tmp_path)
    builder.analyze(directory=src, output=tmp_path)
    builder.close()

    (src / "m.py").write_text("def alpha():\n    return alpha()\n", encoding="utf-8")
    _watch_commit(tmp_path, src / "m.py")

    manifest = json.loads((_current_generation(tmp_path) / "manifest.json").read_text())
    con = sqlite3.connect(
        f"file:{_current_generation(tmp_path) / 'knowledge.db'}?mode=ro", uri=True
    )
    try:
        actual = con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        con.close()
    assert manifest["counts"]["relationships"] == actual
