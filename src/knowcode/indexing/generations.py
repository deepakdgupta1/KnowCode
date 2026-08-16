"""Complete index generations: staging, validation, and atomic publication.

Step 14 of the hardening blueprint, implementing ADR 4. Before it, a full
rebuild mutated the live artifacts in place and in the wrong order::

    shutil.rmtree(index_path)          # the live semantic index is gone
    sqlite_store.bulk_insert(...)      # the new graph is committed anyway
    try:
        self._build_index(...)         # ... and this is allowed to fail
    except Exception as e:
        index_error = str(e)           # reported as a successful analysis

A single failed rebuild therefore published a new graph with no chunk/vector
index and destroyed the last good semantic generation. Reproduced against the
baseline: search returned one result before the failed rebuild and ``[]``
after, while ``knowledge.db`` held the *new* source.

The contract this module implements:

``<index root>/``
    ``generations/<generation id>/``
        ``knowledge.db``, ``chunks.db``, the vector artifacts, and
        ``manifest.json`` — one immutable, self-consistent generation.
    ``current.json``
        The pointer naming the generation readers must use. Published last.
    ``.staging-<generation id>.pid<pid>/``
        A generation under construction. Same directory as its publication
        target, so the finalizing rename stays within one filesystem and is
        therefore atomic. Never read by a reader.

A build stages every artifact without touching the live generation, validates
them *together*, then takes the publication lock, renames the staging directory
into ``generations/``, and atomically replaces ``current.json`` last. Any
failure before that pointer write leaves the previous generation current, and
the staging directory is removed.

**Digests.** SQLite files are validated logically (sorted entity/chunk ID
digests plus counts) rather than by file checksum: opening a WAL database
rewrites its bytes at checkpoint time, so a byte checksum over ``chunks.db``
would false-alarm on the first reader. Immutable artifacts — the vector
metadata envelope, the native vector index, the indexer manifest — are
checksummed by content.

**Locking.** Publication is serialized per artifact root by a process-level
lock. Cross-process exclusion relies on ADR 4's one-writer-process-per-root
rule, the same assumption ``cleanup_orphaned_temp_files`` already makes.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowcode.utils.atomic_write import atomic_write_json
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Directory holding published generations, relative to the index root.
GENERATIONS_DIRNAME = "generations"

#: The pointer naming the generation readers must use.
POINTER_FILENAME = "current.json"

#: Per-generation manifest describing every artifact in it.
MANIFEST_FILENAME = "manifest.json"

#: Prefix marking a generation still under construction.
STAGING_PREFIX = ".staging-"

POINTER_SCHEMA_VERSION = 1

#: Generation manifest schema (ADR 7: "Index manifest ... Schema 3").
MANIFEST_SCHEMA_VERSION = 3

#: How many generations survive publication. Two keeps one last-known-good
#: generation for recovery; each generation is a full copy of the index, so
#: this is also the disk-footprint multiplier.
DEFAULT_RETAINED_GENERATIONS = 2

#: A generation with a graph *and* a semantic index.
KIND_FULL = "full"

#: A generation with only ``knowledge.db``. Publishable when the semantic build
#: fails and no previous semantic generation exists to protect, so a build
#: without usable embeddings still yields a usable graph instead of nothing.
KIND_GRAPH_ONLY = "graph_only"

KNOWLEDGE_DB = "knowledge.db"
CHUNKS_DB = "chunks.db"
VECTOR_METADATA = "vectors.json"
INDEX_MANIFEST = "index_manifest.json"

#: Native vector artifacts, by backend. Exactly one must be present in a full
#: generation; which one depends on the configured backend and engine.
NATIVE_VECTOR_ARTIFACTS = ("vectors.lancedb", "vectors.index", "vectors.npy")

#: The chunk/vector half of a generation: everything a successor inherits when
#: it is derived from an existing generation rather than built from scratch.
SEMANTIC_ARTIFACTS = (
    CHUNKS_DB,
    INDEX_MANIFEST,
    VECTOR_METADATA,
) + NATIVE_VECTOR_ARTIFACTS

_REBUILD_HINT = "Rebuild with `knowcode build`."

_STAGING_NAME = re.compile(
    re.escape(STAGING_PREFIX) + r"(?P<generation>[^.]+)\.pid(?P<pid>\d+)$"
)

_GENERATION_ID = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")

# Generation ids must sort in publication order: retention and pointer
# fallback both pick "the newest". Wall-clock microseconds order builds across
# processes; this guard makes ordering strict within one process, where two
# builds can legitimately land in the same microsecond.
_ID_GUARD = threading.Lock()
_LAST_ID_NUMERIC = 0

# One publication lock per artifact root. Publication is short (a rename plus a
# pointer write), so a plain lock is enough; concurrent *builds* stage into
# separate directories and only serialize at the pointer.
_PUBLICATION_LOCKS: dict[str, threading.Lock] = {}
_PUBLICATION_LOCKS_GUARD = threading.Lock()


class GenerationError(RuntimeError):
    """Base class for index-generation failures."""


class GenerationValidationError(GenerationError):
    """Raised when a staged or retained generation is not self-consistent.

    Carries every failure found rather than the first, so one message names
    the whole problem instead of forcing a repair-and-retry loop.
    """

    def __init__(self, path: Path, failures: Sequence[str]) -> None:
        self.path = path
        self.failures = tuple(failures)
        super().__init__(f"Invalid index generation at {path}: {'; '.join(failures)}")


class GenerationConflictError(GenerationError):
    """Raised when the pointer moved while a generation was being staged.

    A staged generation derived from another one — an incremental build, a
    watch batch (Step 18b) — is only a correct successor to the generation it
    was seeded from. Publishing it after someone else has published would
    silently revert their work, so publication compares the pointer under the
    publication lock and refuses instead. The caller re-derives its changes on
    the new current generation and publishes that.
    """

    def __init__(self, expected: str | None, found: str | None) -> None:
        self.expected = expected
        self.found = found
        super().__init__(
            f"The current index generation changed during staging: expected "
            f"{expected!r}, found {found!r}."
        )


# ----------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactDigest:
    """Content digest for one immutable artifact in a generation."""

    name: str
    kind: str  # "file" | "directory"
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactDigest":
        return cls(
            name=str(payload["name"]),
            kind=str(payload.get("kind", "file")),
            sha256=str(payload["sha256"]),
            bytes=int(payload.get("bytes", 0)),
        )


@dataclass(frozen=True)
class GenerationManifest:
    """Everything needed to prove a generation is complete and self-consistent."""

    schema_version: int
    generation_id: str
    created_at: int
    kind: str
    counts: Mapping[str, int]
    digests: Mapping[str, str]
    embedding: Mapping[str, Any]
    vector: Mapping[str, Any]
    schema_versions: Mapping[str, int]
    artifacts: tuple[ArtifactDigest, ...]

    @property
    def has_semantic_index(self) -> bool:
        """Whether this generation carries a chunk/vector index."""
        return self.kind == KIND_FULL

    def with_counts(self, **counts: int) -> "GenerationManifest":
        """Return a copy with some counts overridden (used by tests/tools)."""
        merged = dict(self.counts)
        merged.update(counts)
        return replace(self, counts=merged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "counts": dict(self.counts),
            "digests": dict(self.digests),
            "embedding": dict(self.embedding),
            "vector": dict(self.vector),
            "schema_versions": dict(self.schema_versions),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GenerationManifest":
        """Parse a manifest payload, failing closed on an unusable one.

        Raises:
            ValueError: The payload is malformed or its schema is unsupported.
        """
        schema_version = payload.get("schema_version")
        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported generation manifest schema version "
                f"{schema_version!r}; expected {MANIFEST_SCHEMA_VERSION}. "
                f"{_REBUILD_HINT}"
            )

        kind = payload.get("kind")
        if kind not in (KIND_FULL, KIND_GRAPH_ONLY):
            raise ValueError(f"Unknown generation kind {kind!r}. {_REBUILD_HINT}")

        generation_id = payload.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError(f"Generation manifest has no id. {_REBUILD_HINT}")

        try:
            artifacts = tuple(
                ArtifactDigest.from_dict(item) for item in payload.get("artifacts", [])
            )
            counts = {str(k): int(v) for k, v in dict(payload["counts"]).items()}
            digests = {str(k): str(v) for k, v in dict(payload["digests"]).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Malformed generation manifest: {exc}. {_REBUILD_HINT}"
            ) from exc

        return cls(
            schema_version=MANIFEST_SCHEMA_VERSION,
            generation_id=generation_id,
            created_at=int(payload.get("created_at", 0)),
            kind=str(kind),
            counts=counts,
            digests=digests,
            embedding=dict(payload.get("embedding", {})),
            vector=dict(payload.get("vector", {})),
            schema_versions={
                str(k): int(v)
                for k, v in dict(payload.get("schema_versions", {})).items()
            },
            artifacts=artifacts,
        )


@dataclass(frozen=True)
class ResolvedGeneration:
    """A validated generation a reader may use."""

    root: Path
    path: Path
    manifest: GenerationManifest

    @property
    def generation_id(self) -> str:
        return self.manifest.generation_id

    @property
    def kind(self) -> str:
        return self.manifest.kind

    @property
    def has_semantic_index(self) -> bool:
        return self.manifest.has_semantic_index

    @property
    def knowledge_db(self) -> Path:
        return self.path / KNOWLEDGE_DB

    @property
    def chunks_db(self) -> Path:
        return self.path / CHUNKS_DB


# ----------------------------------------------------------------------
# Paths and identity
# ----------------------------------------------------------------------


def generations_dir(root: str | Path) -> Path:
    """Directory holding published generations for ``root``."""
    return Path(root) / GENERATIONS_DIRNAME


def pointer_path(root: str | Path) -> Path:
    """Path of the generation pointer for ``root``."""
    return Path(root) / POINTER_FILENAME


def new_generation_id() -> str:
    """Return a fresh, sortable, collision-resistant generation id.

    Lexical order is publication order: retention keeps the newest ids and
    pointer fallback selects the newest valid one, so a random-only suffix
    would make both non-deterministic. The microsecond timestamp orders builds
    across processes and the random suffix prevents collisions.
    """
    global _LAST_ID_NUMERIC
    with _ID_GUARD:
        now = time.time()
        numeric = int(time.strftime("%Y%m%d%H%M%S", time.gmtime(now))) * 1_000_000
        numeric += int((now % 1) * 1_000_000)
        if numeric <= _LAST_ID_NUMERIC:
            # Same microsecond as the previous id in this process; step forward
            # so ordering stays strict.
            numeric = _LAST_ID_NUMERIC + 1
        _LAST_ID_NUMERIC = numeric

    digits = f"{numeric:020d}"  # YYYYMMDD HHMMSS ffffff
    return f"{digits[:8]}T{digits[8:]}Z-{secrets.token_hex(4)}"


def staging_generation_id(staging: str | Path) -> str:
    """Recover the generation id from a staging directory name."""
    match = _STAGING_NAME.match(Path(staging).name)
    if match is None:
        raise ValueError(f"Not a generation staging directory: {staging}")
    return match.group("generation")


def stage_generation(root: str | Path, generation_id: str | None = None) -> Path:
    """Create an empty staging generation beside its publication target.

    The staging directory is a sibling of ``generations/`` inside the same
    artifact root, so the finalizing rename never crosses a filesystem.
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    generations_dir(root_path).mkdir(parents=True, exist_ok=True)

    identifier = generation_id or new_generation_id()
    staging = root_path / f"{STAGING_PREFIX}{identifier}.pid{os.getpid()}"
    staging.mkdir()
    return staging


def discard_staging(staging: str | Path) -> None:
    """Remove a staging generation, ignoring an already-removed directory."""
    shutil.rmtree(Path(staging), ignore_errors=True)


def copy_generation_artifacts(
    source: str | Path, target: str | Path, names: Sequence[str]
) -> list[str]:
    """Copy named artifacts from one generation directory into another.

    A generation derived from another one — an incremental build, a batch of
    watch commits (Step 18b) — starts from a *copy* rather than mutating the
    published original, which ADR 4 makes immutable. The copy is deliberately
    a full one: hardlinking would let a SQLite write in the successor reach the
    published file it was linked to, which is the very defect staging exists to
    prevent.

    Returns:
        The names that were actually present and copied.
    """
    source_dir = Path(source)
    target_dir = Path(target)
    copied: list[str] = []
    for name in names:
        origin = source_dir / name
        if origin.is_dir():
            shutil.copytree(origin, target_dir / name)
        elif origin.exists():
            shutil.copy2(origin, target_dir / name)
        else:
            continue
        copied.append(name)
    return copied


@dataclass
class _StagingHandle:
    """Mutable handle yielded by :func:`staged_generation`."""

    path: Path
    generation_id: str
    published: bool = False


def staged_generation(root: str | Path) -> "_StagedGenerationContext":
    """Context manager creating a staging generation and cleaning it up."""
    return _StagedGenerationContext(Path(root))


class _StagedGenerationContext:
    """Create a staging generation; remove it unless publication claimed it."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._handle: _StagingHandle | None = None

    def __enter__(self) -> _StagingHandle:
        generation_id = new_generation_id()
        path = stage_generation(self._root, generation_id)
        self._handle = _StagingHandle(path=path, generation_id=generation_id)
        return self._handle

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        handle = self._handle
        if handle is None or handle.published:
            return
        discard_staging(handle.path)


# ----------------------------------------------------------------------
# Digests
# ----------------------------------------------------------------------


def digest_ids(ids: Sequence[str]) -> str:
    """Return a stable digest over a set of identifiers.

    Sorted so the digest depends on membership rather than insertion order,
    which is what generation parity actually means.
    """
    hasher = hashlib.sha256()
    for identifier in sorted(set(ids)):
        hasher.update(identifier.encode("utf-8"))
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def _hash_file(path: Path, hasher: "hashlib._Hash") -> int:
    total = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
            total += len(block)
    return total


def digest_artifact(path: Path) -> ArtifactDigest:
    """Digest one immutable artifact, file or directory.

    A directory artifact (LanceDB) hashes each relative path and its content in
    sorted order, so the digest is independent of filesystem iteration order.
    """
    hasher = hashlib.sha256()
    if path.is_dir():
        total = 0
        for entry in sorted(p for p in path.rglob("*") if p.is_file()):
            hasher.update(str(entry.relative_to(path).as_posix()).encode("utf-8"))
            hasher.update(b"\0")
            total += _hash_file(entry, hasher)
        return ArtifactDigest(
            name=path.name, kind="directory", sha256=hasher.hexdigest(), bytes=total
        )

    total = _hash_file(path, hasher)
    return ArtifactDigest(
        name=path.name, kind="file", sha256=hasher.hexdigest(), bytes=total
    )


def digest_artifacts(
    directory: Path, names: Sequence[str]
) -> tuple[ArtifactDigest, ...]:
    """Digest every named artifact that exists in ``directory``."""
    return tuple(
        digest_artifact(directory / name)
        for name in names
        if (directory / name).exists()
    )


# ----------------------------------------------------------------------
# Reading identities back out of a staged/published generation
# ----------------------------------------------------------------------


def _read_ids(db_path: Path, sql: str) -> list[str]:
    """Read one identifier column from a SQLite artifact, read-only.

    Generations are immutable, so validating one must not write to it. A plain
    ``mode=ro`` connection still creates ``-shm``/``-wal`` sidecars for a WAL
    database, so when no write-ahead log is present — the normal case, since
    every store is closed before its generation is digested — the database is
    opened ``immutable=1`` and nothing is created at all. A hot WAL means
    frames may not be merged yet, so that case falls back to ``mode=ro``, where
    reading the current data matters more than leaving no trace.
    """
    hot_wal = db_path.with_name(f"{db_path.name}-wal").exists()
    uri = f"file:{db_path}?mode=ro" if hot_wal else f"file:{db_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return [str(row[0]) for row in conn.execute(sql)]
    finally:
        conn.close()


def read_entity_ids(knowledge_db: Path) -> list[str]:
    """Return every entity id in a generation's knowledge store."""
    return _read_ids(knowledge_db, "SELECT entity_id FROM entities")


def read_chunk_ids(chunks_db: Path) -> list[str]:
    """Return every chunk id in a generation's chunk store."""
    return _read_ids(chunks_db, "SELECT chunk_id FROM chunks")


# ----------------------------------------------------------------------
# Manifest construction and IO
# ----------------------------------------------------------------------


def build_manifest(
    staging: Path,
    *,
    generation_id: str,
    kind: str,
    entity_ids: Sequence[str],
    relationship_count: int,
    chunk_ids: Sequence[str],
    vector_count: int,
    embedding: Mapping[str, Any],
    vector: Mapping[str, Any],
    schema_versions: Mapping[str, int] | None = None,
) -> GenerationManifest:
    """Describe a staged generation, digesting its immutable artifacts."""
    artifact_names: list[str] = []
    if kind == KIND_FULL:
        artifact_names.extend([VECTOR_METADATA, INDEX_MANIFEST])
        artifact_names.extend(NATIVE_VECTOR_ARTIFACTS)

    return GenerationManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        generation_id=generation_id,
        created_at=int(time.time()),
        kind=kind,
        counts={
            "entities": len(entity_ids),
            "relationships": int(relationship_count),
            "chunks": len(chunk_ids),
            "vectors": int(vector_count),
        },
        digests={
            "entity_ids": digest_ids(entity_ids),
            "chunk_ids": digest_ids(chunk_ids),
        },
        embedding=dict(embedding),
        vector=dict(vector),
        schema_versions=dict(schema_versions or {}),
        artifacts=digest_artifacts(staging, artifact_names),
    )


def write_manifest(directory: Path, manifest: GenerationManifest) -> Path:
    """Publish a generation manifest atomically inside ``directory``.

    Called after every artifact it names has been written: data before the
    metadata describing it (Step 13 ordering).
    """
    return atomic_write_json(
        directory / MANIFEST_FILENAME, manifest.to_dict(), indent=2, sort_keys=True
    )


def read_manifest(directory: Path) -> GenerationManifest:
    """Read and validate a generation manifest.

    Raises:
        ValueError: The manifest is missing, truncated, or unsupported.
    """
    import json

    manifest_file = Path(directory) / MANIFEST_FILENAME
    if not manifest_file.exists():
        raise ValueError(
            f"Missing generation manifest {manifest_file}. {_REBUILD_HINT}"
        )
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Invalid or truncated generation manifest {manifest_file}: {exc}. "
            f"{_REBUILD_HINT}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"Generation manifest {manifest_file} is not a JSON object. {_REBUILD_HINT}"
        )
    return GenerationManifest.from_dict(payload)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def validate_generation(
    directory: str | Path,
    *,
    expected_id: str | None = None,
    verify_digests: bool = False,
) -> list[str]:
    """Return every reason ``directory`` is not a usable generation.

    Structural checks (manifest schema, generation id, artifact presence, count
    parity) always run; they are what startup, ``ensure_index()``, and reload
    use, and they are cheap. ``verify_digests`` additionally recomputes the
    entity/chunk id digests and every artifact checksum — used at publication
    time and by ``knowcode doctor``, which are the moments worth the scan.

    An empty list means the generation is complete and self-consistent.
    """
    path = Path(directory)
    if not path.is_dir():
        return [f"missing generation directory {path}. {_REBUILD_HINT}"]

    try:
        manifest = read_manifest(path)
    except ValueError as exc:
        return [str(exc)]

    failures: list[str] = []

    if expected_id is not None and manifest.generation_id != expected_id:
        failures.append(
            f"generation id mismatch: manifest={manifest.generation_id!r} "
            f"directory={expected_id!r}. {_REBUILD_HINT}"
        )

    knowledge_db = path / KNOWLEDGE_DB
    if not knowledge_db.exists():
        failures.append(f"missing artifact {KNOWLEDGE_DB}. {_REBUILD_HINT}")

    if manifest.kind == KIND_FULL:
        chunks_db = path / CHUNKS_DB
        if not chunks_db.exists():
            failures.append(f"missing artifact {CHUNKS_DB}. {_REBUILD_HINT}")

        recorded = {artifact.name for artifact in manifest.artifacts}
        if not recorded & set(NATIVE_VECTOR_ARTIFACTS):
            failures.append(
                "generation records no native vector artifact "
                f"({', '.join(NATIVE_VECTOR_ARTIFACTS)}). {_REBUILD_HINT}"
            )
        for artifact in manifest.artifacts:
            if not (path / artifact.name).exists():
                failures.append(f"missing artifact {artifact.name}. {_REBUILD_HINT}")

        chunks = manifest.counts.get("chunks", 0)
        vectors = manifest.counts.get("vectors", 0)
        if chunks != vectors:
            failures.append(
                f"chunk/vector count mismatch: chunks={chunks} vectors={vectors}. "
                f"{_REBUILD_HINT}"
            )

    if verify_digests:
        failures.extend(_verify_digests(path, manifest))

    return failures


def _verify_digests(path: Path, manifest: GenerationManifest) -> list[str]:
    """Recompute logical id digests and artifact checksums."""
    failures: list[str] = []

    knowledge_db = path / KNOWLEDGE_DB
    if knowledge_db.exists():
        try:
            actual = digest_ids(read_entity_ids(knowledge_db))
        except sqlite3.Error as exc:
            failures.append(f"unreadable {KNOWLEDGE_DB}: {exc}. {_REBUILD_HINT}")
        else:
            if actual != manifest.digests.get("entity_ids"):
                failures.append(
                    f"entity id digest mismatch in {KNOWLEDGE_DB}. {_REBUILD_HINT}"
                )

    chunks_db = path / CHUNKS_DB
    if manifest.kind == KIND_FULL and chunks_db.exists():
        try:
            actual_chunk_ids = read_chunk_ids(chunks_db)
        except sqlite3.Error as exc:
            failures.append(f"unreadable {CHUNKS_DB}: {exc}. {_REBUILD_HINT}")
        else:
            if digest_ids(actual_chunk_ids) != manifest.digests.get("chunk_ids"):
                failures.append(
                    f"chunk id digest mismatch in {CHUNKS_DB}. {_REBUILD_HINT}"
                )
            if len(actual_chunk_ids) != manifest.counts.get("chunks", 0):
                failures.append(
                    f"chunk count mismatch: {CHUNKS_DB} holds "
                    f"{len(actual_chunk_ids)}, manifest records "
                    f"{manifest.counts.get('chunks', 0)}. {_REBUILD_HINT}"
                )

    for artifact in manifest.artifacts:
        artifact_path = path / artifact.name
        if not artifact_path.exists():
            continue  # already reported as missing by the structural pass
        if digest_artifact(artifact_path).sha256 != artifact.sha256:
            failures.append(f"checksum mismatch for {artifact.name}. {_REBUILD_HINT}")

    return failures


# ----------------------------------------------------------------------
# Publication
# ----------------------------------------------------------------------


def _publication_lock(root: Path) -> threading.Lock:
    key = str(root.resolve())
    with _PUBLICATION_LOCKS_GUARD:
        lock = _PUBLICATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PUBLICATION_LOCKS[key] = lock
        return lock


def publish_generation(
    root: str | Path,
    staging: str | Path,
    manifest: GenerationManifest,
    *,
    retain: int = DEFAULT_RETAINED_GENERATIONS,
    protect: Sequence[str] = (),
    expect_current: str | None = None,
) -> ResolvedGeneration:
    """Validate a staged generation and publish it as the current one.

    The sequence is fixed: write the manifest (data before metadata), validate
    the whole staged set including digests, take the publication lock, rename
    the staging directory into ``generations/``, then atomically replace
    ``current.json`` last. A failure at any step before the pointer write
    leaves the previous generation current.

    Args:
        root: The artifact root directory.
        staging: The staging directory containing the new artifacts.
        manifest: The GenerationManifest describing the new generation.
        retain: Number of generations to retain during cleanup.
        protect: Generation ids a reader still holds open. Retention never
            removes these, which is ADR 4's "superseded generations are retired
            only after reader leases end" at the directory level; the service
            supplies them from
            :meth:`~knowcode.service.KnowCodeService.live_generation_ids`.
        expect_current: The generation this staged one was derived from. When
            given, publication refuses if the pointer no longer names it, so a
            successor built from a superseded generation cannot revert whoever
            published in between. ``None`` means the staged generation is
            self-contained — a full build — and needs no such check.

    Raises:
        GenerationValidationError: The staged generation is not complete and
            self-consistent. Nothing is published and the staging directory is
            left for the caller's context manager to discard.
        GenerationConflictError: ``expect_current`` no longer names the current
            generation. Nothing is published.
    """
    root_path = Path(root)
    staging_path = Path(staging)

    write_manifest(staging_path, manifest)

    failures = validate_generation(
        staging_path, expected_id=manifest.generation_id, verify_digests=True
    )
    if failures:
        raise GenerationValidationError(staging_path, failures)

    target = generations_dir(root_path) / manifest.generation_id

    with _publication_lock(root_path):
        if expect_current is not None:
            # Inside the lock, so the window between the check and the rename
            # cannot hold another publication (ADR 4 gives one writer process
            # per artifact root, which is what makes a process-level lock
            # sufficient here).
            pointed = read_pointer(root_path)
            if pointed != expect_current:
                raise GenerationConflictError(expect_current, pointed)
        if target.exists():
            raise GenerationValidationError(
                staging_path,
                [f"generation {manifest.generation_id} already exists at {target}"],
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_path, target)
        _sync_directory(target.parent)

        # Pointer last: until this succeeds no reader can observe the new
        # generation. If it fails, the renamed directory is unreferenced, so it
        # is removed rather than left as a retained generation a later pointer
        # fallback could select.
        try:
            publish_pointer(root_path, manifest)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    resolved = ResolvedGeneration(root=root_path, path=target, manifest=manifest)
    retire_generations(
        root_path,
        keep=retain,
        current=manifest.generation_id,
        protect=protect,
    )
    return resolved


def publish_pointer(root: str | Path, manifest: GenerationManifest) -> Path:
    """Atomically replace the generation pointer.

    The final step of publication, separated so a fault-injection test can fail
    exactly this step and prove that a renamed-but-unpointed generation never
    becomes visible to readers.
    """
    return atomic_write_json(
        pointer_path(root),
        {
            "schema_version": POINTER_SCHEMA_VERSION,
            "generation_id": manifest.generation_id,
            "published_at": int(time.time()),
            "kind": manifest.kind,
        },
        indent=2,
        sort_keys=True,
    )


def _sync_directory(directory: Path) -> None:
    """Persist a directory entry, where the platform supports it."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - platform specific
        logger.debug("Could not open %s to fsync it: %s", directory, exc)
        return
    try:
        os.fsync(descriptor)
    except OSError as exc:  # pragma: no cover - platform specific
        logger.debug("Could not fsync %s: %s", directory, exc)
    finally:
        os.close(descriptor)


# ----------------------------------------------------------------------
# Resolution, retention, and cleanup
# ----------------------------------------------------------------------


def read_pointer(root: str | Path) -> str | None:
    """Return the generation id the pointer names, or ``None`` if unusable."""
    import json

    path = pointer_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Unreadable index generation pointer %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("Index generation pointer %s is not a JSON object", path)
        return None
    if payload.get("schema_version") != POINTER_SCHEMA_VERSION:
        logger.warning(
            "Unsupported index generation pointer version %r in %s",
            payload.get("schema_version"),
            path,
        )
        return None
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        logger.warning("Index generation pointer %s names no generation", path)
        return None
    return generation_id


def list_generations(root: str | Path) -> list[str]:
    """Return every retained generation id, newest id last."""
    directory = generations_dir(root)
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and _GENERATION_ID.match(entry.name)
    )


def resolve_current_generation(
    root: str | Path, *, verify_digests: bool = False
) -> ResolvedGeneration | None:
    """Return the generation readers must use, or ``None`` if there is none.

    The pointed generation wins when it validates. When it does not — an
    unreadable pointer, a deleted directory, a truncated manifest — resolution
    falls back to the newest *completely valid* retained generation rather than
    mixing artifacts (ADR 4 recovery). It never falls back to individual files.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return None

    pointed = read_pointer(root_path)
    candidates: list[str] = []
    if pointed is not None:
        candidates.append(pointed)
    candidates.extend(
        generation_id
        for generation_id in reversed(list_generations(root_path))
        if generation_id != pointed
    )

    for index, generation_id in enumerate(candidates):
        path = generations_dir(root_path) / generation_id
        failures = validate_generation(
            path, expected_id=generation_id, verify_digests=verify_digests
        )
        if not failures:
            if index > 0:
                logger.warning(
                    "Index generation %s is unusable; falling back to %s",
                    pointed if pointed is not None else "<none>",
                    generation_id,
                )
            return ResolvedGeneration(
                root=root_path, path=path, manifest=read_manifest(path)
            )
        logger.debug("Rejected index generation %s: %s", generation_id, failures)

    return None


def retire_generations(
    root: str | Path,
    *,
    keep: int,
    current: str | None = None,
    protect: Sequence[str] = (),
) -> list[Path]:
    """Remove superseded generations, retaining the newest ``keep``.

    The current generation is never removed regardless of ``keep``, so a
    misconfigured retention can degrade recovery depth but never the live
    index. Neither is any generation in ``protect``: a request that is still
    reading a superseded generation holds open file handles inside its
    directory, and removing it underneath that reader is the directory-level
    form of the very race reader leases exist to prevent (Step 18). A protected
    generation is simply removed by a later publication, once nobody holds it.
    """
    root_path = Path(root)
    retained = list_generations(root_path)
    if current is None:
        current = read_pointer(root_path)

    keep = max(1, int(keep))
    survivors = set(retained[-keep:])
    survivors.update(protect)
    if current is not None:
        survivors.add(current)

    removed: list[Path] = []
    for generation_id in retained:
        if generation_id in survivors:
            continue
        path = generations_dir(root_path) / generation_id
        try:
            shutil.rmtree(path)
        except OSError as exc:  # pragma: no cover - platform/permission specific
            logger.warning("Could not retire index generation %s: %s", path, exc)
            continue
        removed.append(path)

    if removed:
        logger.info("Retired %d superseded index generation(s)", len(removed))
    return removed


def cleanup_staging_generations(root: str | Path) -> list[Path]:
    """Remove staging generations abandoned by a crashed writer process.

    Directories staged by *this* process are skipped: another thread may be
    mid-build, and removing its staging directory would turn a live build into
    a failed one. ADR 4 gives one writer process per artifact root, so a
    staging directory from a different PID is by definition abandoned.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    current_pid = os.getpid()
    removed: list[Path] = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        match = _STAGING_NAME.match(entry.name)
        if match is None or int(match.group("pid")) == current_pid:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:  # pragma: no cover - platform/permission specific
            logger.debug("Could not remove abandoned staging %s: %s", entry, exc)
            continue
        removed.append(entry)

    if removed:
        logger.info(
            "Removed %d abandoned index staging generation(s) in %s",
            len(removed),
            root_path,
        )
    return removed
