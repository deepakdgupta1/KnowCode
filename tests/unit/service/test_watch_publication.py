"""Watch commits publish as generations (Step 18b).

Reproduced against the pre-Step-18b service, using its production API only:

* ``service.watch_writer()`` opened its chunk repository and vector store
  *inside the current published generation*, so one watched edit mutated
  artifacts ADR 4 declares immutable. ``validate_generation(...,
  verify_digests=True)`` then reported a chunk-id digest mismatch, a chunk-count
  mismatch, and — on LanceDB — a vector checksum mismatch, and ``knowcode
  doctor`` failed on the result.
* With the FAISS/NumPy backend the same edit was not durable at all:
  ``KnowCodeService.flush()`` correctly refuses to rewrite ``vectors.*`` inside
  a published generation, so a restart found the new chunks in ``chunks.db``
  beside the *old* vector artifact.

The contract this step establishes: a watch commit lands in a staging
generation no reader can see, and becomes visible only when that generation is
published and the service swaps onto it — one atomic move for the whole edit,
exactly like a build.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.indexing import generations
from knowcode.indexing.generation_writer import StagedGenerationWriter
from knowcode.llm.embedding import create_embedding_provider
from knowcode.service import KnowCodeService
from knowcode.service_watch import ServiceWatchWriter


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    """Both vector backends must publish watch commits the same way."""
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _write(root: Path, *names: str) -> Path:
    """Add one source file per name, leaving any others in place."""
    src = root / "src"
    src.mkdir(exist_ok=True)
    for name in names:
        (src / f"{name}.py").write_text(
            f'def {name}():\n    """The {name} routine."""\n    return 1\n',
            encoding="utf-8",
        )
    return src


def _remove(root: Path, *names: str) -> Path:
    """Delete named source files."""
    src = root / "src"
    for name in names:
        (src / f"{name}.py").unlink(missing_ok=True)
    return src


def _publish(root: Path, backend: str) -> str:
    """Publish a generation from the tree as it stands, via a second service."""
    builder = _service(root, backend)
    try:
        stats = builder.analyze(directory=root / "src", output=root)
    finally:
        builder.close()
    assert stats["published"] is True, stats
    generation_id = stats["generation_id"]
    assert isinstance(generation_id, str)
    return generation_id


def _validate(generation: generations.ResolvedGeneration) -> list[str]:
    return generations.validate_generation(
        generation.path,
        expected_id=generation.generation_id,
        verify_digests=True,
    )


def _indexed_files(service: KnowCodeService) -> list[str]:
    with service.generation_lease() as bundle:
        return list(bundle.indexer.chunk_repo.get_all_file_paths())


def _counts(service: KnowCodeService) -> tuple[int, int]:
    with service.generation_lease() as bundle:
        return bundle.indexer.chunk_repo.count(), bundle.indexer.vector_store.count()


# ----------------------------------------------------------------------
# The reproduced defect
# ----------------------------------------------------------------------


def test_a_watched_edit_leaves_the_published_generation_valid(
    tmp_path: Path, backend: str
) -> None:
    """A watched commit must not mutate the generation readers are using."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None
    assert _validate(base) == [], "the build published an invalid generation"

    _write(tmp_path, "beta")
    service.watch_writer().replace_file(src / "beta.py")

    assert _validate(base) == [], (
        "the watched commit invalidated the generation it was committed into"
    )
    service.close()


def test_a_watched_commit_is_durable_across_a_restart(
    tmp_path: Path, backend: str
) -> None:
    """Chunks and vectors must both survive the process that indexed them."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    _write(tmp_path, "beta")
    service.watch_writer().replace_file(src / "beta.py")
    # The server's ownership interface, in shutdown order (Step 17): whatever
    # the watch session holds must be durable by the time flush() returns.
    service.flush()
    service.close()

    restarted = _service(tmp_path, backend)
    try:
        chunks, vectors = _counts(restarted)
        assert any("beta.py" in path for path in _indexed_files(restarted)), (
            "the watched commit did not survive the restart"
        )
        assert vectors == chunks, (
            f"the restart found {chunks} chunks beside {vectors} vectors"
        )
    finally:
        restarted.close()


# ----------------------------------------------------------------------
# Task 3: staged commits, published as one generation
# ----------------------------------------------------------------------


def test_a_watched_commit_publishes_a_new_generation(
    tmp_path: Path, backend: str
) -> None:
    """Publication moves the service onto a complete, valid generation."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    _write(tmp_path, "beta")
    writer = service.watch_writer()
    writer.replace_file(src / "beta.py")
    published = writer.publish_pending()

    assert published is not None and published != base.generation_id
    current = service.current_generation()
    assert current is not None and current.generation_id == published
    assert _validate(current) == []
    assert any("beta.py" in path for path in _indexed_files(service))
    service.close()


def test_the_previous_generation_survives_a_watch_publication(
    tmp_path: Path, backend: str
) -> None:
    """The generation a watch commit was based on stays intact and valid."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None
    before = generations.read_chunk_ids(base.chunks_db)

    _write(tmp_path, "beta")
    writer = service.watch_writer()
    writer.replace_file(src / "beta.py")
    writer.publish_pending()

    assert base.path.is_dir(), "the last known good generation was removed"
    assert generations.read_chunk_ids(base.chunks_db) == before
    assert _validate(base) == []
    service.close()


def test_an_unpublished_commit_is_invisible_to_readers(
    tmp_path: Path, backend: str
) -> None:
    """Staging is not a reader-visible state; publication is the only move."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    _write(tmp_path, "beta")
    writer = service.watch_writer()
    writer.replace_file(src / "beta.py")

    assert not any("beta.py" in path for path in _indexed_files(service))
    current = service.current_generation()
    assert current is not None and current.generation_id == base.generation_id
    assert writer.has_pending
    assert any("beta.py" in path for path in writer.pending_paths())

    writer.publish_pending()
    assert any("beta.py" in path for path in _indexed_files(service))
    assert not writer.has_pending
    service.close()


def test_a_watched_deletion_publishes_a_generation_without_the_file(
    tmp_path: Path, backend: str
) -> None:
    src = _write(tmp_path, "alpha", "beta")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    assert any("beta.py" in path for path in _indexed_files(service))

    _remove(tmp_path, "beta")
    writer = service.watch_writer()
    writer.delete_file(src / "beta.py")
    writer.publish_pending()

    assert not any("beta.py" in path for path in _indexed_files(service))
    current = service.current_generation()
    assert current is not None
    assert _validate(current) == []
    chunks, vectors = _counts(service)
    assert chunks == vectors
    service.close()


def test_publishing_with_nothing_staged_is_a_no_op(
    tmp_path: Path, backend: str
) -> None:
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    writer = service.watch_writer()
    assert writer.publish_pending() is None
    assert not writer.has_pending
    current = service.current_generation()
    assert current is not None and current.generation_id == base.generation_id
    service.close()


# ----------------------------------------------------------------------
# Task 2: publication cadence
# ----------------------------------------------------------------------


def test_a_long_burst_publishes_at_the_batch_cap(tmp_path: Path, backend: str) -> None:
    """A burst that never goes idle still publishes, bounding staleness."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    writer = service.watch_writer()
    writer.max_batch_commits = 2
    for name in ("b0", "b1", "b2"):
        _write(tmp_path, name)
        writer.replace_file(src / f"{name}.py")

    current = service.current_generation()
    assert current is not None
    assert current.generation_id != base.generation_id, (
        "a burst longer than the batch cap never published"
    )
    files = _indexed_files(service)
    assert any("b0.py" in path for path in files)
    assert any("b1.py" in path for path in files)
    assert _validate(current) == []
    # The third commit is still staged; the cap bounds the batch, it does not
    # publish every commit.
    assert writer.has_pending
    service.close()


def test_one_batch_publishes_one_generation(tmp_path: Path, backend: str) -> None:
    """Several commits in one batch cost one staging copy and one publication."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    before = generations.list_generations(tmp_path / "knowcode_index")

    writer = service.watch_writer()
    for name in ("b0", "b1", "b2"):
        _write(tmp_path, name)
        writer.replace_file(src / f"{name}.py")
    writer.publish_pending()

    after = generations.list_generations(tmp_path / "knowcode_index")
    assert len(set(after) - set(before)) == 1
    files = _indexed_files(service)
    for name in ("b0", "b1", "b2"):
        assert any(f"{name}.py" in path for path in files)
    service.close()


# ----------------------------------------------------------------------
# Concurrent publication
# ----------------------------------------------------------------------


def test_a_watch_publication_never_reverts_a_concurrent_rebuild(
    tmp_path: Path, backend: str
) -> None:
    """A full rebuild between staging and publication must not be undone."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    _write(tmp_path, "watched")
    writer = service.watch_writer()
    writer.replace_file(src / "watched.py")

    # A full rebuild publishes underneath the open staging session.
    _write(tmp_path, "rebuilt")
    _publish(tmp_path, backend)
    assert service.reload() is True

    writer.publish_pending()

    files = _indexed_files(service)
    assert any("rebuilt.py" in path for path in files), (
        "the watch publication reverted a concurrent rebuild"
    )
    assert any("watched.py" in path for path in files), (
        "the watched commit was lost to the concurrent rebuild"
    )
    current = service.current_generation()
    assert current is not None
    assert _validate(current) == []
    service.close()


def test_a_rebuild_never_reverts_a_concurrent_watch_publication(
    tmp_path: Path, backend: str
) -> None:
    """A watch publication between a rebuild's scan and its publish must survive.

    The mirror of the test above: there the watch batch had to re-derive over a
    rebuild; here the watch batch has *already published* by the time the
    rebuild's snapshot finishes staging, so its writer holds nothing back to
    re-derive with. Publishing the snapshot unconditionally would revert the
    batch with no record left anywhere. The publication is injected from the
    rebuild's own first progress callback — after its scan, before its publish
    — which makes the losing interleaving deterministic instead of a race.
    """
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None
    writer = service.watch_writer()

    injected = threading.Event()

    def inject_watch_publication(files_done: int, files_total: int | None) -> None:
        if injected.is_set():
            return
        injected.set()
        _write(tmp_path, "bravo")
        writer.replace_file(src / "bravo.py")
        writer.publish_pending()

    stats = service.analyze(
        directory=src, output=tmp_path, on_progress=inject_watch_publication
    )

    assert injected.is_set(), "the watch publication never landed mid-build"
    assert stats["published"] is True, stats
    current = service.current_generation()
    assert current is not None
    assert current.generation_id != base.generation_id
    assert _validate(current) == []
    files = _indexed_files(service)
    assert any("alpha.py" in path for path in files)
    assert any("bravo.py" in path for path in files), (
        "the rebuild reverted a watch publication that landed mid-build"
    )
    assert not writer.has_pending
    service.close()


def test_concurrent_commits_and_publications_converge(
    tmp_path: Path, backend: str
) -> None:
    """Sustained contention may refuse a publication, but never lose one.

    A refused publication keeps its batch, so the next one re-derives and
    republishes it — which is exactly what the worker does on its next drain.
    The invariant is convergence, not the absence of contention.
    """
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    writer = service.watch_writer()

    started = threading.Barrier(2, timeout=60)
    errors: list[BaseException] = []
    conflicts: list[BaseException] = []

    def commit() -> None:
        try:
            started.wait()
            for index in range(4):
                _write(tmp_path, f"w{index}")
                writer.replace_file(src / f"w{index}.py")
                try:
                    writer.publish_pending()
                except generations.GenerationConflictError as exc:
                    # The documented outcome under contention: retained, and
                    # republished below.
                    conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    def rebuild() -> None:
        try:
            started.wait()
            for _ in range(2):
                _publish(tmp_path, backend)
                service.reload()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    threads = [threading.Thread(target=target) for target in (commit, rebuild)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
        assert not thread.is_alive(), "a worker thread did not finish in time"

    assert not errors, f"a concurrent commit or publication failed: {errors!r}"
    writer.publish_pending()
    assert not writer.has_pending, (
        f"work stayed unpublished after {len(conflicts)} conflict(s)"
    )

    current = service.current_generation()
    assert current is not None
    assert _validate(current) == []
    files = _indexed_files(service)
    for index in range(4):
        assert any(f"w{index}.py" in path for path in files), (
            f"w{index}.py never converged into a published generation"
        )
    service.close()


# ----------------------------------------------------------------------
# Installs with no published generation
# ----------------------------------------------------------------------


def test_a_commit_without_a_published_generation_stays_in_place(
    tmp_path: Path, backend: str
) -> None:
    """The flat layout is mutable, so it needs no staging and no publication."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    assert service.current_generation() is None

    writer = service.watch_writer()
    writer.replace_file(src / "alpha.py")

    assert writer.publish_pending() is None
    assert not writer.has_pending
    assert service.current_generation() is None
    assert any("alpha.py" in path for path in _indexed_files(service))

    service.flush()
    service.close()

    # The flat layout keeps its vectors because the chunk rows carry them; the
    # plane is rebuilt on the next open rather than saved beside them.
    restarted = _service(tmp_path, backend)
    assert restarted.get_indexer().vector_store.count() >= 1, (
        "the flat layout lost its vectors across a restart"
    )
    restarted.close()


# ----------------------------------------------------------------------
# Fail-closed boundaries of the staged writer
# ----------------------------------------------------------------------


def _staging_dirs(root: Path) -> list[Path]:
    index_root = root / "knowcode_index"
    if not index_root.is_dir():
        return []
    return [
        entry
        for entry in index_root.iterdir()
        if entry.is_dir() and entry.name.startswith(generations.STAGING_PREFIX)
    ]


def test_a_graph_only_generation_cannot_be_succeeded_by_a_watch_batch(
    tmp_path: Path, backend: str
) -> None:
    """There is no chunk/vector generation to derive one from."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    graph_only = generations.ResolvedGeneration(
        root=base.root,
        path=base.path,
        manifest=replace(base.manifest, kind=generations.KIND_GRAPH_ONLY),
    )
    with pytest.raises(ValueError, match="no semantic index"):
        StagedGenerationWriter(
            index_root=service.index_root,
            base=graph_only,
            provider=create_embedding_provider(app_config=service.app_config),
            backend=backend,
        )
    service.close()


def test_a_writer_that_cannot_open_its_staging_leaves_nothing_behind(
    tmp_path: Path, backend: str
) -> None:
    """A half-seeded staging directory is not a generation and must not linger."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None
    # A truncated index manifest fails closed on load (Step 13), which is the
    # first thing the writer does after copying.
    (base.path / "index_manifest.json").write_text("{ truncated", encoding="utf-8")

    with pytest.raises(ValueError):
        StagedGenerationWriter(
            index_root=service.index_root,
            base=base,
            provider=create_embedding_provider(app_config=service.app_config),
            backend=backend,
        )
    assert _staging_dirs(tmp_path) == []
    service.close()


def test_a_consumed_writer_refuses_further_work(tmp_path: Path, backend: str) -> None:
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    writer = StagedGenerationWriter(
        index_root=service.index_root,
        base=base,
        provider=create_embedding_provider(app_config=service.app_config),
        backend=backend,
    )
    writer.discard()
    assert writer.is_closed
    writer.discard()  # idempotent

    with pytest.raises(RuntimeError, match="published or discarded"):
        writer.replace_file(src / "alpha.py")
    with pytest.raises(RuntimeError, match="published or discarded"):
        writer.publish(expect_current=base.generation_id)
    assert _staging_dirs(tmp_path) == []
    service.close()


def test_a_store_that_will_not_close_does_not_strand_the_staging(
    tmp_path: Path, backend: str
) -> None:
    """Losing one connection must not also leak the directory and the other."""
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None

    writer = StagedGenerationWriter(
        index_root=service.index_root,
        base=base,
        provider=create_embedding_provider(app_config=service.app_config),
        backend=backend,
    )
    vector_store = writer.indexer.vector_store

    def refuse() -> None:
        raise OSError("the chunk repository refused to close")

    writer.indexer.chunk_repo.close = refuse  # type: ignore[method-assign]
    writer.discard()

    assert _staging_dirs(tmp_path) == []
    assert vector_store.count() == 0, "the vector store was left open"
    service.close()


def test_a_batch_cap_below_one_is_rejected(tmp_path: Path, backend: str) -> None:
    service = _service(tmp_path, backend)
    with pytest.raises(ValueError, match="at least 1"):
        ServiceWatchWriter(service, max_batch_commits=0)
    service.close()


def test_a_batch_whose_base_generation_vanished_fails_closed(
    tmp_path: Path, backend: str
) -> None:
    """An empty successor must never be published as if it replaced a full one.

    A base directory that is gone copies *nothing*, and every store opened on
    the staging directory afterwards would helpfully create an empty one. That
    would publish an empty generation over a populated one, so seeding refuses.
    """
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    _write(tmp_path, "beta")
    writer = service.watch_writer()
    writer.replace_file(src / "beta.py")

    # Every artifact a reader could resolve a generation from, removed under
    # the open batch: the publication conflicts, and re-deriving finds nothing
    # to succeed.
    index_root = tmp_path / "knowcode_index"
    generations.pointer_path(index_root).unlink(missing_ok=True)
    shutil.rmtree(generations.generations_dir(index_root), ignore_errors=True)

    with pytest.raises(generations.GenerationValidationError) as caught:
        writer.publish_pending()
    assert "missing artifact knowledge.db" in str(caught.value)
    # Retained rather than dropped: the drain report names it as work that is
    # not in the index.
    assert writer.has_pending
    assert any("beta.py" in path for path in writer.pending_paths())
    assert _staging_dirs(tmp_path) == []
    service.close()


def test_a_watch_publication_advances_the_graph_for_the_file_it_touched(
    tmp_path: Path, backend: str
) -> None:
    """A file transaction rewrites chunks, vectors, *and* the file's graph rows.

    This test used to pin the opposite, deliberately: the watch worker had
    never updated ``knowledge.db``, so a published watch generation carried
    the graph of the build it succeeded, and the docstring said a test that
    quietly accepted either behavior would let that be "fixed" by accident.
    It was fixed on purpose (BL-17), so the assertions are inverted rather
    than relaxed — the file's entities now reach the graph, the entities of
    every other file are untouched, and the generation still validates whole.
    """
    src = _write(tmp_path, "alpha")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)
    base = service.current_generation()
    assert base is not None
    entities_before = set(generations.read_entity_ids(base.knowledge_db))

    _write(tmp_path, "beta")
    writer = service.watch_writer()
    writer.replace_file(src / "beta.py")
    writer.publish_pending()

    current = service.current_generation()
    assert current is not None
    entities_after = set(generations.read_entity_ids(current.knowledge_db))
    # Strictly additive: beta's rows arrived, alpha's stayed exactly as they were.
    assert entities_before < entities_after
    assert {eid for eid in entities_after - entities_before if "beta.py" in eid}
    assert not {eid for eid in entities_after - entities_before if "alpha.py" in eid}
    # The count describes the artifact rather than the base it was seeded from.
    assert current.manifest.counts["relationships"] == generations.count_relationships(
        current.knowledge_db
    )
    # The chunk half moved too, and the generation still validates as a whole.
    assert any("beta.py" in path for path in _indexed_files(service))
    assert _validate(current) == []
    assert service.search("beta") != [], "the graph half did not advance"
    service.close()


def test_a_watch_publication_compacts_without_losing_chunks(
    tmp_path: Path, backend: str
) -> None:
    """The watch path vacuums its staged store, so it needs the same witness.

    Every count and digest the manifest records is read out of ``chunks.db``
    after :meth:`publish` compacts it, so a lossy rewrite would produce a
    manifest that agrees with the damage (BL-8). The staged repository's own
    row count, taken before ``publish`` runs, is the one number that does not
    come from the compacted file.
    """
    src = _write(tmp_path, "alpha", "beta")
    service = _service(tmp_path, backend)
    service.analyze(directory=src, output=tmp_path)

    _write(tmp_path, "gamma")
    writer = service.watch_writer()
    writer.replace_file(src / "gamma.py")
    session = writer._session
    assert session is not None, "precondition: the commit opened a staging writer"
    staged_chunks = session.indexer.chunk_repo.count()
    assert staged_chunks > 0, "precondition: the batch staged something"

    published = writer.publish_pending()
    assert published is not None

    current = service.current_generation()
    assert current is not None and current.generation_id == published
    assert current.manifest.counts["chunks"] == staged_chunks
    assert current.manifest.counts["vectors"] == staged_chunks
    assert _validate(current) == []
    assert any("gamma.py" in path for path in _indexed_files(service))
    service.close()
