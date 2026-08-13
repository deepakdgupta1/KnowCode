"""Full-rebuild generation publication contract (Step 14).

The reviewed defect: ``analyze()`` deleted the live semantic index before its
replacement existed, committed ``knowledge.db`` before attempting the semantic
rebuild, and reported a failed index build as a successful analysis. A single
failed rebuild therefore published a new graph with no chunk/vector index and
destroyed the last good semantic generation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from knowcode.config import AppConfig
from knowcode.indexing import generations
from knowcode.indexing.generations import (
    POINTER_FILENAME,
    list_generations,
    resolve_current_generation,
)
from knowcode.indexing.indexer import Indexer
from knowcode.service import KnowCodeService


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    """Both vector backends must obey the same publication contract."""
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _source_tree(root: Path, body: str = "return 1") -> Path:
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "m.py").write_text(f"def alpha():\n    {body}\n", encoding="utf-8")
    return src


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _search(service: KnowCodeService, query: str = "alpha") -> list[str]:
    engine = service.get_search_engine()
    return [chunk.entity_id for chunk in engine.search(query, limit=5)]


# ----------------------------------------------------------------------
# The reviewed defect
# ----------------------------------------------------------------------


def test_failed_semantic_rebuild_preserves_the_previous_generation(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild that fails after the graph is built must publish nothing.

    Before Step 14 this reproduced as: ``analyze()`` returned successfully with
    only ``index_error`` set, the live semantic index directory was gone, the
    new graph was committed anyway, and search returned no results.
    """
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    before = _search(service)
    assert before, "precondition: the first generation is searchable"
    first_generation = resolve_current_generation(tmp_path / "knowcode_index")
    assert first_generation is not None

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")

    def explode(self: Indexer, root_dir: Any, **kwargs: Any) -> int:
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(Indexer, "index_directory", explode)

    rebuilt = _service(tmp_path, backend)
    stats = rebuilt.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert stats["index_error"], "a failed semantic build must be reported"
    assert stats["published"] is False

    # The previous complete generation is still the current one.
    current = resolve_current_generation(tmp_path / "knowcode_index")
    assert current is not None
    assert current.generation_id == first_generation.generation_id

    restarted = _service(tmp_path, backend)
    assert _search(restarted) == before

    # And the new graph was NOT committed alongside the old index.
    entity = restarted.store.get_entity(f"{(src / 'm.py').resolve()}::alpha")
    assert entity is not None
    assert "return 1" in (entity.source_code or "")


# ----------------------------------------------------------------------
# Successful publication
# ----------------------------------------------------------------------


def test_analyze_publishes_all_four_artifact_classes_together(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)

    stats = service.analyze(directory=src, output=tmp_path)

    assert stats["published"] is True
    index_root = tmp_path / "knowcode_index"
    resolved = resolve_current_generation(index_root)
    assert resolved is not None
    assert resolved.kind == generations.KIND_FULL
    assert resolved.knowledge_db.exists()
    assert resolved.chunks_db.exists()
    assert (resolved.path / "vectors.json").exists()
    assert (resolved.path / generations.MANIFEST_FILENAME).exists()
    assert resolved.manifest.counts["chunks"] == resolved.manifest.counts["vectors"]
    assert resolved.manifest.counts["entities"] >= 1
    assert stats["generation_id"] == resolved.generation_id


def test_published_generation_is_the_store_and_index_the_service_reads(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    restarted = _service(tmp_path, backend)

    assert restarted._store_file() == resolved.knowledge_db
    assert restarted.get_indexer().chunk_repo._db_path == resolved.chunks_db
    assert _search(restarted)


def test_a_second_build_publishes_a_new_generation_and_retires_nothing_live(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    rebuilt = _service(tmp_path, backend)
    rebuilt.analyze(directory=src, output=tmp_path)

    second = resolve_current_generation(tmp_path / "knowcode_index")
    assert second.generation_id != first.generation_id

    restarted = _service(tmp_path, backend)
    entity = restarted.store.get_entity(f"{(src / 'm.py').resolve()}::alpha")
    assert "return 2" in (entity.source_code or "")


# ----------------------------------------------------------------------
# Failure injection at each publication boundary
# ----------------------------------------------------------------------


def _fail_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowcode.indexing.graph_builder import GraphBuilder

    def explode(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(GraphBuilder, "build_from_directory", explode)


def _injection_points() -> list[tuple[str, Callable[[pytest.MonkeyPatch], None]]]:
    def fail_knowledge_write(monkeypatch: pytest.MonkeyPatch) -> None:
        from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore

        def explode(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("knowledge store write failed")

        monkeypatch.setattr(SqliteKnowledgeStore, "bulk_insert", explode)

    def fail_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(self: Indexer, *args: Any, **kwargs: Any) -> int:
            raise RuntimeError("embedding provider unavailable")

        monkeypatch.setattr(Indexer, "index_directory", explode)

    def fail_vector_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(self: Indexer, *args: Any, **kwargs: Any) -> None:
            raise OSError("vector artifact write failed")

        monkeypatch.setattr(Indexer, "save", explode)

    def fail_manifest_write(monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: Any, **kwargs: Any) -> None:
            raise OSError("manifest write failed")

        monkeypatch.setattr(generations, "write_manifest", explode)

    def fail_pointer_publish(monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail the very last step, after the staged directory was renamed."""

        def explode(*args: Any, **kwargs: Any) -> None:
            raise OSError("pointer write failed")

        monkeypatch.setattr(generations, "publish_pointer", explode)

    return [
        ("knowledge_write", fail_knowledge_write),
        ("embedding", fail_embedding),
        ("vector_persistence", fail_vector_persistence),
        ("manifest_write", fail_manifest_write),
        ("pointer_publish", fail_pointer_publish),
    ]


def test_a_graph_parse_failure_propagates_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan/parse crash is a defect, not a publication outcome.

    Nothing has been staged at that point, so there is no artifact story to
    classify; the caller gets the real traceback and the prior generation is
    untouched.
    """
    src = _source_tree(tmp_path)
    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)
    before = _search(service)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    _fail_parsing(monkeypatch)
    with pytest.raises(RuntimeError, match="scanner exploded"):
        _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert (
        resolve_current_generation(tmp_path / "knowcode_index").generation_id
        == first.generation_id
    )
    assert _search(_service(tmp_path, "faiss")) == before


@pytest.mark.parametrize(
    "label,inject", _injection_points(), ids=[name for name, _ in _injection_points()]
)
def test_every_pre_publication_failure_preserves_the_prior_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    inject: Callable[[pytest.MonkeyPatch], None],
) -> None:
    backend = "faiss"
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    before = _search(service)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    inject(monkeypatch)

    rebuilt = _service(tmp_path, backend)
    stats = rebuilt.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert stats["published"] is False, f"{label} must not publish"
    current = resolve_current_generation(tmp_path / "knowcode_index")
    assert current is not None
    assert current.generation_id == first.generation_id

    restarted = _service(tmp_path, backend)
    assert _search(restarted) == before


@pytest.mark.parametrize(
    "label,inject", _injection_points(), ids=[name for name, _ in _injection_points()]
)
def test_a_failed_build_leaves_no_staging_generation_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    inject: Callable[[pytest.MonkeyPatch], None],
) -> None:
    src = _source_tree(tmp_path)
    inject(monkeypatch)

    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    index_root = tmp_path / "knowcode_index"
    staged = (
        [
            path
            for path in index_root.iterdir()
            if path.name.startswith(generations.STAGING_PREFIX)
        ]
        if index_root.exists()
        else []
    )
    assert staged == [], f"{label} left a staging generation behind"


def test_first_build_failure_publishes_nothing_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no prior generation, a publication failure still leaves nothing."""
    src = _source_tree(tmp_path)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("pointer write failed")

    monkeypatch.setattr(generations, "publish_pointer", explode)

    service = _service(tmp_path, "faiss")
    stats = service.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert stats["published"] is False
    assert stats["index_error_stage"] == "publication"
    assert resolve_current_generation(tmp_path / "knowcode_index") is None
    assert list_generations(tmp_path / "knowcode_index") == []


# ----------------------------------------------------------------------
# Classified failures
# ----------------------------------------------------------------------


def test_semantic_build_failure_is_classified_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _source_tree(tmp_path)

    def explode(self: Indexer, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(Indexer, "index_directory", explode)

    service = _service(tmp_path, "faiss")
    stats = service.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert stats["index_error"]
    assert stats["index_error_stage"] == "semantic_index"
    assert stats["published"] is True, (
        "with no previous semantic generation to protect, the graph is still "
        "published as an explicit graph-only generation"
    )
    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    assert resolved is not None
    assert resolved.kind == generations.KIND_GRAPH_ONLY
    assert resolved.has_semantic_index is False


def test_graph_only_generation_never_replaces_a_full_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")
    assert first.kind == generations.KIND_FULL

    def explode(self: Indexer, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(Indexer, "index_directory", explode)
    rebuilt = _service(tmp_path, "faiss")
    stats = rebuilt.analyze(directory=src, output=tmp_path)
    monkeypatch.undo()

    assert stats["published"] is False
    assert (
        resolve_current_generation(tmp_path / "knowcode_index").generation_id
        == first.generation_id
    )


# ----------------------------------------------------------------------
# ensure_index, restart, reload
# ----------------------------------------------------------------------


def test_ensure_index_publishes_a_generation_when_none_exists(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)

    service.ensure_index(directory=src)

    assert resolve_current_generation(tmp_path / "knowcode_index") is not None


def test_ensure_index_reuses_a_valid_published_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    _service(tmp_path, backend).ensure_index(directory=src)

    assert (
        resolve_current_generation(tmp_path / "knowcode_index").generation_id
        == first.generation_id
    )


def test_ensure_index_rebuilds_when_the_pointed_generation_is_incomplete(
    tmp_path: Path, backend: str
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    resolved.chunks_db.unlink()

    rebuilt = _service(tmp_path, backend)
    rebuilt.ensure_index(directory=src)

    current = resolve_current_generation(tmp_path / "knowcode_index")
    assert current is not None
    assert current.generation_id != resolved.generation_id
    assert current.chunks_db.exists()


def test_reload_moves_the_service_onto_the_newly_published_generation(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    reader = _service(tmp_path, "faiss")
    reader.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    reader.reload()

    second = resolve_current_generation(tmp_path / "knowcode_index")
    assert second.generation_id != first.generation_id
    assert reader._store_file() == second.knowledge_db
    assert reader.get_indexer().chunk_repo._db_path == second.chunks_db
    entity = reader.store.get_entity(f"{(src / 'm.py').resolve()}::alpha")
    assert "return 2" in (entity.source_code or "")


def test_restart_ignores_an_unpublished_staging_generation(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)
    resolved = resolve_current_generation(tmp_path / "knowcode_index")

    index_root = tmp_path / "knowcode_index"
    orphan = index_root / f"{generations.STAGING_PREFIX}20200101T000000Z-abcdef.pid1"
    orphan.mkdir()
    (orphan / "knowledge.db").write_bytes(b"junk")

    restarted = _service(tmp_path, "faiss")
    assert restarted._store_file() == resolved.knowledge_db
    assert _search(restarted)


def test_pointer_naming_a_missing_generation_falls_back_to_a_valid_one(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, "faiss")
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "m.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    second = resolve_current_generation(tmp_path / "knowcode_index")

    import shutil

    shutil.rmtree(second.path)

    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    assert resolved is not None
    assert resolved.generation_id == first.generation_id
    assert _search(_service(tmp_path, "faiss"))


def test_retention_keeps_the_last_known_good_generation_on_disk(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    for index in range(3):
        (src / "m.py").write_text(
            f"def alpha():\n    return {index}\n", encoding="utf-8"
        )
        _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    retained = list_generations(tmp_path / "knowcode_index")
    assert len(retained) == generations.DEFAULT_RETAINED_GENERATIONS
    assert resolve_current_generation(tmp_path / "knowcode_index") is not None


# ----------------------------------------------------------------------
# Legacy layouts
# ----------------------------------------------------------------------


def test_legacy_flat_layout_is_still_readable_when_no_pointer_exists(
    tmp_path: Path,
) -> None:
    """Pre-Step-14 installs have no pointer; they must not become unreadable."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}", encoding="utf-8")
    service = _service(tmp_path, "faiss")

    assert service._store_file() == tmp_path / "knowcode_knowledge.json"
    indexer = service.get_indexer()
    try:
        assert indexer.chunk_repo._db_path == tmp_path / "knowcode_index" / "chunks.db"
    finally:
        indexer.chunk_repo.close()


def test_pointer_json_is_human_readable(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    pointer = json.loads(
        (tmp_path / "knowcode_index" / POINTER_FILENAME).read_text(encoding="utf-8")
    )
    assert set(pointer) >= {"schema_version", "generation_id", "published_at"}
