"""Contract tests for complete index generations (Step 14).

ADR 4 requires that a full or incremental build stage every artifact in a
unique directory, validate them together, and publish one atomic pointer last.
These tests exercise the staging/validation/publication primitive directly; the
service-level rebuild that uses it lives in
``tests/unit/service/test_generation_publication.py``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from knowcode.indexing import generations
from knowcode.indexing.generations import (
    GenerationValidationError,
    GenerationManifest,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    cleanup_staging_generations,
    generations_dir,
    list_generations,
    new_generation_id,
    pointer_path,
    publish_generation,
    resolve_current_generation,
    stage_generation,
    validate_generation,
)


# ----------------------------------------------------------------------
# Helpers: a minimal but structurally complete staged generation
# ----------------------------------------------------------------------


def _write_ids(path: Path, table: str, column: str, ids: list[str]) -> None:
    """Create a SQLite artifact matching the real store's identifier column."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({column} TEXT PRIMARY KEY)")
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO {table} ({column}) VALUES (?)", [(i,) for i in ids]
        )
        conn.commit()
    finally:
        conn.close()


def _write_chunks(path: Path, ids: list[str], *, embedded: int | None = None) -> None:
    """Create a chunks artifact carrying durable embeddings, as a build does.

    ``embedded`` is how many rows keep a non-NULL ``embedding``; the rest are
    stored but absent from the semantic plane.
    """
    embedded = len(ids) if embedded is None else embedded
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks "
            "(chunk_id TEXT PRIMARY KEY, embedding BLOB, embedding_dim INTEGER)"
        )
        conn.execute("DELETE FROM chunks")
        conn.executemany(
            "INSERT INTO chunks (chunk_id, embedding, embedding_dim) VALUES (?, ?, ?)",
            [
                (
                    cid,
                    b"\x00\x00\x80?" if i < embedded else None,
                    1 if i < embedded else None,
                )
                for i, cid in enumerate(ids)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _stage(
    root: Path,
    *,
    entity_ids: list[str] | None = None,
    chunk_ids: list[str] | None = None,
    kind: str = generations.KIND_FULL,
    builder: dict | None = None,
) -> tuple[Path, GenerationManifest]:
    """Stage a structurally complete generation and return its path/manifest."""
    entity_ids = ["m.py::alpha"] if entity_ids is None else entity_ids
    chunk_ids = ["m.py::alpha::0"] if chunk_ids is None else chunk_ids

    staging = stage_generation(root)
    _write_ids(staging / "knowledge.db", "entities", "entity_id", entity_ids)
    if kind == generations.KIND_FULL:
        _write_chunks(staging / "chunks.db", chunk_ids)
        (staging / "index_manifest.json").write_text(
            json.dumps({"schema_version": 2, "embedding": {"dimension": 4}}),
            encoding="utf-8",
        )

    manifest = generations.build_manifest(
        staging,
        generation_id=generations.staging_generation_id(staging),
        kind=kind,
        entity_ids=entity_ids,
        relationship_count=0,
        chunk_ids=chunk_ids if kind == generations.KIND_FULL else [],
        vector_count=len(chunk_ids) if kind == generations.KIND_FULL else 0,
        embedding={"provider": "dummy", "model_name": "m", "dimension": 4},
        vector={"backend": "faiss", "dimension": 4, "schema_version": 3},
        builder=builder,
    )
    generations.write_manifest(staging, manifest)
    return staging, manifest


# ----------------------------------------------------------------------
# Staging
# ----------------------------------------------------------------------


def test_generation_ids_are_unique(tmp_path: Path) -> None:
    ids = {new_generation_id() for _ in range(50)}
    assert len(ids) == 50


def test_generation_ids_sort_in_publication_order() -> None:
    """Retention and pointer fallback both mean "the newest id"."""
    ids = [new_generation_id() for _ in range(50)]

    assert ids == sorted(ids)


def test_generation_ids_stay_ordered_within_one_microsecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two builds can land in the same microsecond; order must still be strict."""
    monkeypatch.setattr(generations.time, "time", lambda: 1_780_000_000.123456)

    ids = [new_generation_id() for _ in range(5)]

    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_staging_directory_lives_beside_the_generations_directory(
    tmp_path: Path,
) -> None:
    """Staging must share the filesystem with its publication target."""
    staging = stage_generation(tmp_path)

    assert staging.is_dir()
    assert staging.parent == tmp_path
    assert staging.name.startswith(generations.STAGING_PREFIX)
    # Nothing is published yet.
    assert not pointer_path(tmp_path).exists()


def test_staging_never_touches_the_live_generation(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    published = publish_generation(tmp_path, staging, manifest)

    live_bytes = (published.path / "knowledge.db").read_bytes()
    second, _ = _stage(tmp_path, entity_ids=["m.py::beta"])

    assert second.exists()
    assert (published.path / "knowledge.db").read_bytes() == live_bytes
    assert resolve_current_generation(tmp_path).generation_id == published.generation_id


# ----------------------------------------------------------------------
# Publication
# ----------------------------------------------------------------------


def test_publish_moves_the_staged_directory_and_writes_the_pointer_last(
    tmp_path: Path,
) -> None:
    staging, manifest = _stage(tmp_path)

    resolved = publish_generation(tmp_path, staging, manifest)

    assert not staging.exists()
    assert resolved.path == generations_dir(tmp_path) / resolved.generation_id
    assert (resolved.path / "knowledge.db").exists()
    assert (resolved.path / MANIFEST_FILENAME).exists()

    pointer = json.loads(pointer_path(tmp_path).read_text(encoding="utf-8"))
    assert pointer["generation_id"] == resolved.generation_id
    assert pointer["schema_version"] == generations.POINTER_SCHEMA_VERSION


def test_pointer_write_failure_leaves_the_previous_generation_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at the last publication step must not retire the old pointer."""
    first_staging, first_manifest = _stage(tmp_path)
    first = publish_generation(tmp_path, first_staging, first_manifest)

    second_staging, second_manifest = _stage(tmp_path, entity_ids=["m.py::beta"])

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(generations, "publish_pointer", explode)

    with pytest.raises(OSError):
        publish_generation(tmp_path, second_staging, second_manifest)

    monkeypatch.undo()
    assert resolve_current_generation(tmp_path).generation_id == first.generation_id
    # The renamed-but-unpointed directory is rolled back, so a later pointer
    # fallback cannot select a generation that was never published.
    assert list_generations(tmp_path) == [first.generation_id]


def test_publish_rejects_an_invalid_staged_generation(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    (staging / "index_manifest.json").unlink()

    with pytest.raises(GenerationValidationError) as excinfo:
        publish_generation(tmp_path, staging, manifest)

    assert "index_manifest.json" in str(excinfo.value)
    assert not pointer_path(tmp_path).exists()


def test_publish_of_an_invalid_generation_keeps_the_previous_one_searchable(
    tmp_path: Path,
) -> None:
    first_staging, first_manifest = _stage(tmp_path)
    first = publish_generation(tmp_path, first_staging, first_manifest)

    bad_staging, bad_manifest = _stage(tmp_path, entity_ids=["m.py::beta"])
    (bad_staging / "chunks.db").unlink()

    with pytest.raises(GenerationValidationError):
        publish_generation(tmp_path, bad_staging, bad_manifest)

    assert resolve_current_generation(tmp_path).generation_id == first.generation_id


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def test_validation_rejects_a_manifest_claiming_vectors_chunks_db_lacks(
    tmp_path: Path,
) -> None:
    """The manifest's vector count must describe the durable rows on disk."""
    staging, manifest = _stage(tmp_path)
    broken = manifest.with_counts(vectors=manifest.counts["chunks"] + 1)
    generations.write_manifest(staging, broken)

    failures = validate_generation(staging, expected_id=broken.generation_id)

    assert any("durable embedding" in failure for failure in failures), failures


def test_validation_rejects_a_chunk_id_digest_mismatch(tmp_path: Path) -> None:
    """A chunk store whose membership drifted from the manifest is not usable."""
    staging, manifest = _stage(tmp_path, chunk_ids=["a", "b"])
    _write_ids(staging / "chunks.db", "chunks", "chunk_id", ["a", "c"])

    failures = validate_generation(
        staging, expected_id=manifest.generation_id, verify_digests=True
    )

    assert any("chunk id digest mismatch" in failure for failure in failures)


def test_validation_rejects_an_entity_id_digest_mismatch(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path, entity_ids=["m.py::alpha"])
    _write_ids(staging / "knowledge.db", "entities", "entity_id", ["m.py::beta"])

    failures = validate_generation(
        staging, expected_id=manifest.generation_id, verify_digests=True
    )

    assert any("entity id digest mismatch" in failure for failure in failures)


def test_validation_rejects_a_tampered_artifact_checksum(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    (staging / "index_manifest.json").write_text("{}", encoding="utf-8")

    failures = validate_generation(
        staging, expected_id=manifest.generation_id, verify_digests=True
    )

    assert any("checksum" in failure for failure in failures)


def test_validation_rejects_a_generation_id_mismatch(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)

    failures = validate_generation(staging, expected_id="not-this-generation")

    assert any("generation id" in failure for failure in failures)


def test_validation_fails_closed_on_an_unsupported_manifest_schema(
    tmp_path: Path,
) -> None:
    staging, manifest = _stage(tmp_path)
    payload = json.loads((staging / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    payload["schema_version"] = MANIFEST_SCHEMA_VERSION + 1
    (staging / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    failures = validate_generation(staging, expected_id=manifest.generation_id)

    assert any("schema" in failure for failure in failures)
    assert any("knowcode build" in failure for failure in failures)


def test_validation_fails_closed_on_a_truncated_manifest(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    (staging / MANIFEST_FILENAME).write_text('{"schema_version": 3', encoding="utf-8")

    failures = validate_generation(staging, expected_id=manifest.generation_id)

    assert failures
    assert any("knowcode build" in failure for failure in failures)


def test_graph_only_generation_is_valid_without_semantic_artifacts(
    tmp_path: Path,
) -> None:
    staging, manifest = _stage(tmp_path, kind=generations.KIND_GRAPH_ONLY)

    assert validate_generation(staging, expected_id=manifest.generation_id) == []

    resolved = publish_generation(tmp_path, staging, manifest)
    assert resolved.kind == generations.KIND_GRAPH_ONLY
    assert resolved.has_semantic_index is False


# ----------------------------------------------------------------------
# Resolution, fallback, and retention
# ----------------------------------------------------------------------


def test_resolve_returns_none_when_nothing_was_ever_published(tmp_path: Path) -> None:
    assert resolve_current_generation(tmp_path) is None


def test_resolve_falls_back_to_the_last_valid_generation(tmp_path: Path) -> None:
    """An unreadable pointer may only select a *complete* retained generation."""
    first_staging, first_manifest = _stage(tmp_path)
    first = publish_generation(tmp_path, first_staging, first_manifest)

    second_staging, second_manifest = _stage(tmp_path, entity_ids=["m.py::beta"])
    second = publish_generation(tmp_path, second_staging, second_manifest)

    # Corrupt the newest generation and point at nothing readable.
    (second.path / MANIFEST_FILENAME).write_text("{ truncated", encoding="utf-8")
    pointer_path(tmp_path).write_text("{ truncated", encoding="utf-8")

    resolved = resolve_current_generation(tmp_path)

    assert resolved is not None
    assert resolved.generation_id == first.generation_id


def test_resolve_returns_none_when_no_retained_generation_is_valid(
    tmp_path: Path,
) -> None:
    staging, manifest = _stage(tmp_path)
    published = publish_generation(tmp_path, staging, manifest)
    (published.path / "knowledge.db").unlink()

    assert resolve_current_generation(tmp_path) is None


def test_resolve_never_mixes_artifacts_from_two_generations(tmp_path: Path) -> None:
    first_staging, first_manifest = _stage(tmp_path, chunk_ids=["a", "b"])
    first = publish_generation(tmp_path, first_staging, first_manifest)
    second_staging, second_manifest = _stage(tmp_path, chunk_ids=["c"])
    second = publish_generation(tmp_path, second_staging, second_manifest)

    resolved = resolve_current_generation(tmp_path)

    assert resolved.generation_id == second.generation_id
    assert resolved.knowledge_db.parent == resolved.path
    assert resolved.chunks_db.parent == resolved.path
    assert resolved.path != first.path


def test_retention_keeps_the_last_known_good_generation(tmp_path: Path) -> None:
    published = []
    for index in range(4):
        staging, manifest = _stage(tmp_path, entity_ids=[f"m.py::e{index}"])
        published.append(publish_generation(tmp_path, staging, manifest, retain=2))

    remaining = set(list_generations(tmp_path))

    assert remaining == {published[-1].generation_id, published[-2].generation_id}
    assert (
        resolve_current_generation(tmp_path).generation_id
        == published[-1].generation_id
    )


def test_retention_never_removes_the_current_generation(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    published = publish_generation(tmp_path, staging, manifest, retain=1)

    assert list_generations(tmp_path) == [published.generation_id]
    assert resolve_current_generation(tmp_path) is not None


def test_cleanup_removes_abandoned_staging_directories_from_other_processes(
    tmp_path: Path,
) -> None:
    mine = stage_generation(tmp_path)
    abandoned = tmp_path / f"{generations.STAGING_PREFIX}20200101T000000Z-deadbe.pid1"
    abandoned.mkdir()
    (abandoned / "chunks.db").write_bytes(b"junk")

    removed = cleanup_staging_generations(tmp_path)

    assert abandoned not in [path for path in removed] or not abandoned.exists()
    assert not abandoned.exists()
    assert mine.exists(), "a staging directory owned by this process must survive"


def test_staging_directory_name_carries_the_owning_pid(tmp_path: Path) -> None:
    staging = stage_generation(tmp_path)

    assert f".pid{os.getpid()}" in staging.name


def test_cleanup_ignores_a_root_that_does_not_exist(tmp_path: Path) -> None:
    assert cleanup_staging_generations(tmp_path / "absent") == []


def test_staging_generation_id_rejects_a_non_staging_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="staging directory"):
        generations.staging_generation_id(tmp_path / "generations")


# ----------------------------------------------------------------------
# Fail-closed parsing of manifests and pointers
# ----------------------------------------------------------------------


def _manifest_payload(tmp_path: Path) -> dict:
    staging, _ = _stage(tmp_path)
    return json.loads((staging / MANIFEST_FILENAME).read_text(encoding="utf-8"))


def test_manifest_rejects_an_unknown_generation_kind(tmp_path: Path) -> None:
    payload = _manifest_payload(tmp_path)
    payload["kind"] = "partial"

    with pytest.raises(ValueError, match="Unknown generation kind"):
        generations.GenerationManifest.from_dict(payload)


def test_manifest_rejects_a_missing_generation_id(tmp_path: Path) -> None:
    payload = _manifest_payload(tmp_path)
    payload["generation_id"] = ""

    with pytest.raises(ValueError, match="no id"):
        generations.GenerationManifest.from_dict(payload)


def test_manifest_rejects_malformed_counts(tmp_path: Path) -> None:
    payload = _manifest_payload(tmp_path)
    payload["counts"] = {"chunks": "many"}

    with pytest.raises(ValueError, match="Malformed generation manifest"):
        generations.GenerationManifest.from_dict(payload)


def test_read_manifest_reports_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing generation manifest"):
        generations.read_manifest(tmp_path)


def test_read_manifest_rejects_a_non_object_payload(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="not a JSON object"):
        generations.read_manifest(tmp_path)


def test_validation_reports_an_unreadable_chunk_store(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    (staging / "chunks.db").write_bytes(b"not a database")

    failures = validate_generation(
        staging, expected_id=manifest.generation_id, verify_digests=True
    )

    assert any("unreadable chunks.db" in failure for failure in failures)


def test_validation_reports_an_unreadable_knowledge_store(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    (staging / "knowledge.db").write_bytes(b"not a database")

    failures = validate_generation(
        staging, expected_id=manifest.generation_id, verify_digests=True
    )

    assert any("unreadable knowledge.db" in failure for failure in failures)


def test_validation_reports_a_chunk_count_that_disagrees_with_the_manifest(
    tmp_path: Path,
) -> None:
    staging, manifest = _stage(tmp_path, chunk_ids=["a", "b"])
    broken = manifest.with_counts(chunks=5, vectors=5)
    generations.write_manifest(staging, broken)

    failures = validate_generation(
        staging, expected_id=broken.generation_id, verify_digests=True
    )

    assert any("chunk count mismatch" in failure for failure in failures)


def test_publish_refuses_to_overwrite_an_existing_generation(tmp_path: Path) -> None:
    staging, manifest = _stage(tmp_path)
    publish_generation(tmp_path, staging, manifest)

    replay, _ = _stage(tmp_path)
    with pytest.raises(generations.GenerationValidationError, match="already exists"):
        publish_generation(tmp_path, replay, manifest)


@pytest.mark.parametrize(
    "payload",
    ["[]", json.dumps({"generation_id": "x"}), json.dumps({"schema_version": 1})],
    ids=["not-an-object", "no-version", "no-generation-id"],
)
def test_an_unusable_pointer_names_no_generation(tmp_path: Path, payload: str) -> None:
    pointer_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pointer_path(tmp_path).write_text(payload, encoding="utf-8")

    assert generations.read_pointer(tmp_path) is None


def test_retention_without_an_explicit_current_reads_the_pointer(
    tmp_path: Path,
) -> None:
    published = []
    for index in range(3):
        staging, manifest = _stage(tmp_path, entity_ids=[f"m.py::e{index}"])
        published.append(publish_generation(tmp_path, staging, manifest, retain=99))

    removed = generations.retire_generations(tmp_path, keep=1)

    assert len(removed) == 2
    assert list_generations(tmp_path) == [published[-1].generation_id]


# ----------------------------------------------------------------------
# Reader-lease protection (Step 18, ADR 4)
# ----------------------------------------------------------------------


def test_retention_keeps_a_generation_a_reader_still_holds(tmp_path: Path) -> None:
    """A leased generation survives retention until its reader releases.

    Removing a directory whose ``chunks.db`` a request still has open is the
    directory-level form of the race reader leases exist to prevent, so the
    service passes its live generation ids through publication.
    """
    published = []
    for index in range(3):
        staging, manifest = _stage(tmp_path, entity_ids=[f"m.py::e{index}"])
        published.append(publish_generation(tmp_path, staging, manifest, retain=99))
    leased = published[0].generation_id

    removed = generations.retire_generations(tmp_path, keep=1, protect=[leased])

    assert [path.name for path in removed] == [published[1].generation_id]
    assert list_generations(tmp_path) == sorted([leased, published[-1].generation_id])


def test_a_protected_generation_is_removed_once_it_is_released(
    tmp_path: Path,
) -> None:
    published = []
    for index in range(3):
        staging, manifest = _stage(tmp_path, entity_ids=[f"m.py::e{index}"])
        published.append(publish_generation(tmp_path, staging, manifest, retain=99))
    leased = published[0].generation_id

    generations.retire_generations(tmp_path, keep=1, protect=[leased])
    generations.retire_generations(tmp_path, keep=1)

    assert list_generations(tmp_path) == [published[-1].generation_id]


def test_publication_protects_the_generations_it_is_given(tmp_path: Path) -> None:
    """The protection reaches retention through ``publish_generation``."""
    first = publish_generation(tmp_path, *_stage(tmp_path, entity_ids=["m.py::a"]))
    second = publish_generation(tmp_path, *_stage(tmp_path, entity_ids=["m.py::b"]))
    third = publish_generation(
        tmp_path,
        *_stage(tmp_path, entity_ids=["m.py::c"]),
        retain=1,
        protect=[first.generation_id],
    )

    assert first.path.is_dir(), "a leased generation was removed by retention"
    assert not second.path.is_dir()
    assert third.path.is_dir()


# ----------------------------------------------------------------------
# The vector plane is derived, not published
# ----------------------------------------------------------------------


def test_manifest_digests_no_native_vector_artifact(tmp_path: Path) -> None:
    """A published generation carries chunks and graph, never an ANN index.

    The index is rebuildable from the durable embeddings in ``chunks.db``, so
    publishing it costs a third of a generation for bytes already on disk.
    """
    staging, manifest = _stage(tmp_path)

    digested = {artifact.name for artifact in manifest.artifacts}

    assert digested.isdisjoint(set(generations.NATIVE_VECTOR_ARTIFACTS))
    assert generations.VECTOR_METADATA not in digested


def test_full_generation_validates_without_a_vector_artifact(tmp_path: Path) -> None:
    staging, _ = _stage(tmp_path)

    assert validate_generation(staging, verify_digests=True) == []


def test_validation_fails_when_chunks_lost_their_durable_embeddings(
    tmp_path: Path,
) -> None:
    """The guard now compares the manifest against the artifact, not itself.

    ``chunks == vectors`` compared two numbers in the same file and could not
    see a generation whose rows had lost their vectors.
    """
    chunk_ids = ["m.py::alpha::0", "m.py::beta::0", "m.py::gamma::0"]
    staging, _ = _stage(tmp_path, chunk_ids=chunk_ids)
    _write_chunks(staging / "chunks.db", chunk_ids, embedded=1)

    failures = validate_generation(staging)

    assert any("durable embedding" in failure for failure in failures), failures


def test_semantic_artifacts_do_not_carry_a_vector_plane_forward(tmp_path: Path) -> None:
    """Incremental builds seed from this set; copying 32 MB of cache is waste."""
    assert set(generations.SEMANTIC_ARTIFACTS).isdisjoint(
        set(generations.NATIVE_VECTOR_ARTIFACTS)
    )


# ----------------------------------------------------------------------
# Builder fingerprint stamp (BL-32: a store must name the code that built it)
# ----------------------------------------------------------------------


def test_build_manifest_stamps_the_builder_when_given(tmp_path: Path) -> None:
    staging, _ = _stage(tmp_path)
    manifest = generations.build_manifest(
        staging,
        generation_id=generations.staging_generation_id(staging),
        kind=generations.KIND_GRAPH_ONLY,
        entity_ids=[],
        relationship_count=0,
        chunk_ids=[],
        vector_count=0,
        embedding={},
        vector={},
        builder={"code_fingerprint": "sha256:abc", "code_files": 7},
    )
    payload = manifest.to_dict()
    assert payload["builder"] == {"code_fingerprint": "sha256:abc", "code_files": 7}
    assert generations.GenerationManifest.from_dict(payload).builder == {
        "code_fingerprint": "sha256:abc",
        "code_files": 7,
    }


def test_manifest_without_a_builder_keeps_its_exact_shape(tmp_path: Path) -> None:
    """The stamp is additive: unstamped manifests stay byte-for-byte familiar.

    Readers older than the stamp ignore the key, and this reader treats its
    absence as predates fingerprinting rather than as corruption.
    """
    staging, _ = _stage(tmp_path)
    manifest = generations.build_manifest(
        staging,
        generation_id=generations.staging_generation_id(staging),
        kind=generations.KIND_GRAPH_ONLY,
        entity_ids=[],
        relationship_count=0,
        chunk_ids=[],
        vector_count=0,
        embedding={},
        vector={},
    )
    assert "builder" not in manifest.to_dict()
    assert manifest.builder == {}
    assert generations.GenerationManifest.from_dict(manifest.to_dict()).builder == {}


def test_manifest_rejects_a_malformed_builder(tmp_path: Path) -> None:
    payload = _manifest_payload(tmp_path)
    payload["builder"] = "sha256:abc"

    with pytest.raises(ValueError, match="Malformed generation manifest"):
        generations.GenerationManifest.from_dict(payload)


def test_current_builder_drift_names_a_mismatched_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_root = tmp_path / "knowcode_index"
    staging, manifest = _stage(
        index_root,
        kind=generations.KIND_GRAPH_ONLY,
        builder={"code_fingerprint": "sha256:built", "code_files": 3},
    )
    generations.publish_generation(index_root, staging, manifest)

    monkeypatch.setattr(
        generations, "package_code_fingerprint", lambda: "sha256:running"
    )
    drift = generations.current_builder_drift(index_root)
    assert drift is not None
    assert "sha256:built" in drift
    assert "sha256:running" in drift


def test_current_builder_drift_is_silent_when_code_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_root = tmp_path / "knowcode_index"
    staging, manifest = _stage(
        index_root,
        kind=generations.KIND_GRAPH_ONLY,
        builder={"code_fingerprint": "sha256:same", "code_files": 3},
    )
    generations.publish_generation(index_root, staging, manifest)

    monkeypatch.setattr(generations, "package_code_fingerprint", lambda: "sha256:same")
    assert generations.current_builder_drift(index_root) is None


def test_current_builder_drift_is_silent_without_a_generation(tmp_path: Path) -> None:
    assert generations.current_builder_drift(tmp_path / "knowcode_index") is None
