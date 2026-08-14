"""Incremental builds publish generations, never mutate the live one (Step 15).

Step 14 proved a *full* rebuild is all-or-nothing. These exercise the
incremental path — the one watched edits and `knowcode build --incremental`
take — through the real service on a real git repository, for both vector
backends: a new generation is published, the previous one is left byte-for-byte
alone, a failure anywhere before publication keeps the previous generation
searchable, and chunk/vector membership stays equal across edits and deletions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from knowcode.config import AppConfig
from knowcode.indexing import generations
from knowcode.indexing.generations import digest_artifact, resolve_current_generation
from knowcode.indexing.indexer import Indexer
from knowcode.service import KnowCodeService


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


def _repository(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    (src / "alpha.py").write_text(
        "def alpha():\n    return 1\n\n\ndef helper():\n    return 2\n",
        encoding="utf-8",
    )
    (src / "beta.py").write_text("def beta():\n    return 3\n", encoding="utf-8")

    _git(src, "init")
    _git(src, "config", "user.email", "test@example.com")
    _git(src, "config", "user.name", "Test")
    _git(src, "add", ".")
    _git(src, "commit", "-m", "initial")
    return src


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _commit(src: Path, message: str) -> None:
    _git(src, "add", "-A")
    _git(src, "commit", "-m", message)


def _counts(generation: Any) -> tuple[int, int]:
    return (
        generation.manifest.counts["chunks"],
        generation.manifest.counts["vectors"],
    )


def _index_root(root: Path) -> Path:
    return root / "knowcode_index"


# ----------------------------------------------------------------------
# Publication
# ----------------------------------------------------------------------


def test_an_incremental_build_publishes_a_new_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None

    (src / "gamma.py").write_text("def gamma():\n    return 4\n", encoding="utf-8")
    _commit(src, "add gamma")

    stats = _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )

    assert stats["published"] is True
    second = resolve_current_generation(_index_root(tmp_path))
    assert second is not None
    assert second.generation_id != first.generation_id
    assert _counts(second)[0] == _counts(second)[1]

    searched = _service(tmp_path, backend).get_search_engine().search("gamma", limit=5)
    assert any("gamma" in chunk.entity_id for chunk in searched)


def test_an_incremental_build_never_mutates_the_published_generation(
    tmp_path: Path, backend: str
) -> None:
    """The live generation is immutable: readers on it see a stable index."""
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None
    before = digest_artifact(first.path / "manifest.json").sha256
    before_chunks = set(generations.read_chunk_ids(first.chunks_db))

    (src / "alpha.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")
    _commit(src, "edit alpha")
    _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )

    assert first.path.is_dir(), "the previous generation is retained for recovery"
    assert digest_artifact(first.path / "manifest.json").sha256 == before
    assert set(generations.read_chunk_ids(first.chunks_db)) == before_chunks


def test_a_failed_incremental_build_leaves_the_previous_generation_searchable(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None
    before = [
        chunk.entity_id
        for chunk in _service(tmp_path, backend)
        .get_search_engine()
        .search("alpha", limit=5)
    ]
    assert before

    (src / "alpha.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")
    _commit(src, "edit alpha")

    def explode(self: Indexer, root_dir: Any, **kwargs: Any) -> int:
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(Indexer, "index_incremental", explode)
    stats = _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )
    monkeypatch.undo()

    assert stats["published"] is False
    assert stats["index_error_stage"] == "semantic_index"

    current = resolve_current_generation(_index_root(tmp_path))
    assert current is not None
    assert current.generation_id == first.generation_id

    restarted = _service(tmp_path, backend)
    assert [
        chunk.entity_id for chunk in restarted.get_search_engine().search("alpha", limit=5)
    ] == before


# ----------------------------------------------------------------------
# Membership parity across edits and deletions
# ----------------------------------------------------------------------


def test_repeated_incremental_builds_keep_chunk_and_vector_counts_equal(
    tmp_path: Path, backend: str
) -> None:
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)

    for iteration in range(3):
        (src / "alpha.py").write_text(
            f"def alpha():\n    return {iteration}\n", encoding="utf-8"
        )
        _commit(src, f"edit {iteration}")
        stats = _service(tmp_path, backend).analyze(
            directory=src, output=tmp_path, incremental=True
        )
        assert stats["published"] is True

        generation = resolve_current_generation(_index_root(tmp_path))
        assert generation is not None
        chunks, vectors = _counts(generation)
        assert chunks == vectors
        assert chunks == len(generations.read_chunk_ids(generation.chunks_db))


def test_a_deleted_file_leaves_no_chunks_or_vectors_behind(
    tmp_path: Path, backend: str
) -> None:
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None
    before_chunks, _ = _counts(first)

    (src / "beta.py").unlink()
    _commit(src, "drop beta")

    stats = _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )
    assert stats["published"] is True

    generation = resolve_current_generation(_index_root(tmp_path))
    assert generation is not None
    chunks, vectors = _counts(generation)
    assert chunks == vectors
    assert chunks < before_chunks
    assert not any(
        "beta.py" in chunk_id
        for chunk_id in generations.read_chunk_ids(generation.chunks_db)
    )


# ----------------------------------------------------------------------
# Failure injection at every commit boundary
# ----------------------------------------------------------------------


def test_a_manifest_write_failure_publishes_nothing(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last write of the semantic phase is still pre-publication."""
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None

    (src / "alpha.py").write_text("def alpha():\n    return 7\n", encoding="utf-8")
    _commit(src, "edit alpha")

    import knowcode.indexing.indexer as indexer_module

    def explode(*args: Any, **kwargs: Any) -> Path:
        raise OSError("no space left on device")

    monkeypatch.setattr(indexer_module, "atomic_write_json", explode)
    stats = _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )
    monkeypatch.undo()

    assert stats["published"] is False
    current = resolve_current_generation(_index_root(tmp_path))
    assert current is not None
    assert current.generation_id == first.generation_id


def test_an_unrecoverable_vector_commit_failure_publishes_nothing(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vector failure after the chunk commit discards the staged generation."""
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None

    (src / "alpha.py").write_text("def alpha():\n    return 8\n", encoding="utf-8")
    _commit(src, "edit alpha")

    def explode(self: Indexer, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("vector backend unavailable")

    def no_recovery(self: Indexer, *args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(Indexer, "_apply_vector_generation", explode)
    monkeypatch.setattr(Indexer, "_recover_vectors_from_chunks", no_recovery)
    stats = _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )
    monkeypatch.undo()

    assert stats["published"] is False
    assert stats["index_error_stage"] == "semantic_index"

    current = resolve_current_generation(_index_root(tmp_path))
    assert current is not None
    assert current.generation_id == first.generation_id
    assert [
        chunk.entity_id
        for chunk in _service(tmp_path, backend).get_search_engine().search("alpha", limit=5)
    ]


def test_a_failed_incremental_build_leaves_no_staging_directory(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery point: a restart after a failure sees only complete state."""
    src = _repository(tmp_path)
    _service(tmp_path, backend).analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(_index_root(tmp_path))
    assert first is not None

    (src / "alpha.py").write_text("def alpha():\n    return 9\n", encoding="utf-8")
    _commit(src, "edit alpha")

    def explode(self: Indexer, root_dir: Any, **kwargs: Any) -> int:
        raise RuntimeError("crash mid-build")

    monkeypatch.setattr(Indexer, "index_incremental", explode)
    _service(tmp_path, backend).analyze(
        directory=src, output=tmp_path, incremental=True
    )
    monkeypatch.undo()

    staged = [
        entry
        for entry in _index_root(tmp_path).iterdir()
        if entry.name.startswith(generations.STAGING_PREFIX)
    ]
    assert staged == []

    restarted = _service(tmp_path, backend)
    resolved = restarted.current_generation()
    assert resolved is not None
    assert resolved.generation_id == first.generation_id
