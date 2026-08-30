"""Freshness sees a deleted source file (BL-23).

Staleness was derived entirely from ``max(mtime)`` across files that
*currently exist*. Deleting a source file raises no file's mtime, so
``is_stale`` stayed false while the index kept serving entities for a file
that was gone -- and no file count or path set was compared, so nothing else
could catch it either.

Both sides of the comparison are covered here. ``indexed_file_paths`` has to
return paths in the same normalized form the scanner's do, or the difference
between the two sets is meaningless whichever way it comes out.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from knowcode.config import AppConfig
from knowcode.data_models import Entity, EntityKind, Location
from knowcode.service import KnowCodeService
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.utils.entity_identity import normalize_file_identity


def _entity(path: Path, name: str) -> Entity:
    return Entity(
        id=f"{normalize_file_identity(path)}::{name}",
        kind=EntityKind.FUNCTION,
        name=name,
        qualified_name=name,
        location=Location(str(path), 1, 2),
        source_code=f"def {name}(): ...",
    )


def _store_over(tmp_path: Path, files: list[Path]) -> SqliteKnowledgeStore:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    for index, path in enumerate(files):
        store.add_entity(_entity(path, f"fn{index}"))
    return store


def test_indexed_file_paths_are_comparable_with_scanned_ones(tmp_path: Path) -> None:
    """The producer half: normalized absolute POSIX, the scanner's own form.

    A set difference between two spellings of the same file would report every
    file as deleted, so this is the assertion that makes the consumer's
    comparison mean anything.
    """
    source = tmp_path / "src" / "m.py"
    source.parent.mkdir(parents=True)
    source.write_text("def fn0(): ...\n")

    store = _store_over(tmp_path, [source])
    try:
        assert store.indexed_file_paths() == {normalize_file_identity(source)}
    finally:
        store.close()


def _service_over(tmp_path: Path, store: SqliteKnowledgeStore) -> KnowCodeService:
    store_file = tmp_path / "knowcode_knowledge.json"
    store_file.write_text("{}", encoding="utf-8")
    index_dir = tmp_path / "knowcode_index"
    index_dir.mkdir(exist_ok=True)
    (index_dir / "index_manifest.json").write_text("{}", encoding="utf-8")

    service = KnowCodeService(store_path=tmp_path, app_config=AppConfig())
    service._store_root = MagicMock(return_value=tmp_path / "src")
    service._store_file = MagicMock(return_value=store_file)
    service._index_path = MagicMock(return_value=index_dir)
    service._current_bundle = MagicMock(return_value=SimpleNamespace(store=store))
    return service


def test_a_deleted_source_file_makes_the_index_stale(tmp_path: Path) -> None:
    """The consumer half, and the defect itself."""
    src = tmp_path / "src"
    src.mkdir()
    kept, removed = src / "kept.py", src / "removed.py"
    kept.write_text("def fn0(): ...\n")
    removed.write_text("def fn1(): ...\n")

    store = _store_over(tmp_path, [kept, removed])
    try:
        service = _service_over(tmp_path, store)
        assert service.get_freshness_metadata()["is_stale"] is False

        removed.unlink()

        freshness = service.get_freshness_metadata()
        assert freshness["is_stale"] is True
        assert "store_stale_files_deleted" in freshness["stale_reasons"]
    finally:
        store.close()


def test_a_file_the_index_never_covered_does_not_read_as_a_deletion(
    tmp_path: Path,
) -> None:
    """Over-rejection guard, and the reason counts would not have worked.

    A scanned file with no entities -- one the parser rejected -- makes the
    indexed set *smaller* than the scanned set permanently. Comparing sizes
    would have reported that as a deletion for the life of the index; the
    difference in the one direction that matters does not.
    """
    src = tmp_path / "src"
    src.mkdir()
    indexed = src / "indexed.py"
    indexed.write_text("def fn0(): ...\n")
    (src / "unparsed.py").write_text("(((\n")

    store = _store_over(tmp_path, [indexed])
    try:
        freshness = _service_over(tmp_path, store).get_freshness_metadata()
        assert "store_stale_files_deleted" not in freshness["stale_reasons"]
    finally:
        store.close()


def test_a_store_that_cannot_anchor_its_paths_invents_no_deletions(
    tmp_path: Path,
) -> None:
    """The failure mode that would have made this signal worse than useless.

    ``_load_id`` re-anchors a stored path onto the repository root, but a store
    with no recorded root returns what was written -- and a relative path
    resolves against the process working directory, not the repository. Every
    indexed file would then be missing from the scan, and every retrieval would
    report a wholesale deletion. Unanchored paths are dropped instead: this
    signal can miss a deletion, which is what already shipped, but it must
    never invent one.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "present.py").write_text("def fn0(): ...\n")

    store = SqliteKnowledgeStore(tmp_path / "knowledge.db")
    try:
        store.add_entity(
            Entity(
                id="present.py::fn0",
                kind=EntityKind.FUNCTION,
                name="fn0",
                qualified_name="fn0",
                location=Location("present.py", 1, 2),
            )
        )
        assert store.indexed_file_paths() == set()

        freshness = _service_over(tmp_path, store).get_freshness_metadata()
        assert "store_stale_files_deleted" not in freshness["stale_reasons"]
    finally:
        store.close()
