"""Step 13 contract tests for the shared crash-safe artifact writer.

The reviewed defect: ``KnowledgeStore.save``, the FAISS/LanceDB metadata
envelopes, and the index manifest all truncate their target in place
(``open(path, "w")`` then ``json.dump``). A failure — serialization, a full
disk, a killed process — therefore destroys the previous valid artifact and
leaves a truncated file that no loader can read.

The contract this suite pins down (ADR 4, invariant 7): serialize first, write
into a *same-directory* temporary file, flush, ``fsync`` the file, ``os.replace``
onto the target, then ``fsync`` the parent directory where the platform
supports it. Any raised failure leaves the previous file byte-identical and no
temporary file behind.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

import pytest

from knowcode.utils import atomic_write
from knowcode.utils.atomic_write import (
    TEMP_SUFFIX,
    atomic_replacement,
    atomic_write_json,
    cleanup_orphaned_temp_files,
)

PREVIOUS = {"generation": 1, "count": 7}
NEXT = {"generation": 2, "count": 9}


def _seed(path: Path, payload: dict[str, Any] = PREVIOUS) -> Path:
    """Write a previous, valid artifact directly (not through the writer)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _temp_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.name.endswith(TEMP_SUFFIX))


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


# --- Happy path -----------------------------------------------------------


def test_write_replaces_the_target_with_the_serialized_payload(tmp_path: Path) -> None:
    """The published file holds exactly the new payload."""
    target = _seed(tmp_path / "vectors.json")

    atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == NEXT


def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    """A staged generation directory need not exist yet."""
    target = tmp_path / "generations" / "g1" / "manifest.json"

    atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == NEXT


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """A successful publication cleans up after itself."""
    target = tmp_path / "vectors.json"

    atomic_write_json(target, NEXT)

    assert _temp_files(tmp_path) == []


def test_temporary_file_is_created_in_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-filesystem temp directory would make ``os.replace`` non-atomic."""
    target = tmp_path / "nested" / "vectors.json"
    seen: list[tuple[Path, Path]] = []

    real_replace = atomic_write._replace

    def spy(source: Path, destination: Path) -> None:
        seen.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write, "_replace", spy)
    atomic_write_json(target, NEXT)

    (source, destination) = seen[0]
    assert source.parent == destination.parent == target.parent
    assert source.name.endswith(TEMP_SUFFIX)


def test_durability_sequence_is_write_flush_fsync_replace_then_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the whole guarantee: a replace before fsync publishes garbage."""
    target = tmp_path / "vectors.json"
    order: list[str] = []

    real_write = atomic_write._write_text
    real_fsync_file = atomic_write._fsync_file
    real_replace = atomic_write._replace
    real_fsync_dir = atomic_write._fsync_directory

    def write(handle: TextIO, text: str) -> None:
        order.append("write")
        real_write(handle, text)

    def fsync_file(handle: TextIO) -> None:
        order.append("fsync_file")
        real_fsync_file(handle)

    def replace(source: Path, destination: Path) -> None:
        order.append("replace")
        real_replace(source, destination)

    def fsync_dir(directory: Path) -> None:
        order.append("fsync_dir")
        real_fsync_dir(directory)

    monkeypatch.setattr(atomic_write, "_write_text", write)
    monkeypatch.setattr(atomic_write, "_fsync_file", fsync_file)
    monkeypatch.setattr(atomic_write, "_replace", replace)
    monkeypatch.setattr(atomic_write, "_fsync_directory", fsync_dir)

    atomic_write_json(target, NEXT)

    assert order == ["write", "fsync_file", "replace", "fsync_dir"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_written_file_has_the_documented_mode(tmp_path: Path) -> None:
    """Mode is explicit, not inherited from ``mkstemp``'s 0600 or the umask."""
    target = tmp_path / "vectors.json"

    atomic_write_json(target, NEXT)

    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_written_file_honours_an_explicit_restrictive_mode(tmp_path: Path) -> None:
    """A caller with a private artifact can demand 0600."""
    target = tmp_path / "secret.json"

    atomic_write_json(target, NEXT, mode=0o600)

    assert target.stat().st_mode & 0o777 == 0o600


# --- Fault injection: the previous file must survive ----------------------


def test_serialization_failure_preserves_the_previous_file(tmp_path: Path) -> None:
    """Serializing before opening any file keeps an unencodable payload harmless."""
    target = _seed(tmp_path / "vectors.json")

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": {1, 2, 3}})

    assert json.loads(target.read_text(encoding="utf-8")) == PREVIOUS
    assert _temp_files(tmp_path) == []


def test_short_write_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk that fills mid-write must not truncate the live artifact."""
    target = _seed(tmp_path / "vectors.json")

    def short_write(handle: TextIO, text: str) -> None:
        handle.write(text[: len(text) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(atomic_write, "_write_text", short_write)

    with pytest.raises(OSError):
        atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == PREVIOUS
    assert _temp_files(tmp_path) == []


def test_file_fsync_failure_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unflushable temp file is never promoted onto the target."""
    target = _seed(tmp_path / "vectors.json")
    monkeypatch.setattr(atomic_write, "_fsync_file", _boom)

    with pytest.raises(OSError):
        atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == PREVIOUS
    assert _temp_files(tmp_path) == []


def test_replace_failure_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename leaves the old generation live, not a half-written one."""
    target = _seed(tmp_path / "vectors.json")
    monkeypatch.setattr(atomic_write, "_replace", _boom)

    with pytest.raises(OSError):
        atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == PREVIOUS
    assert _temp_files(tmp_path) == []


def test_directory_fsync_failure_is_not_fatal_after_a_successful_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement already happened; raising would misreport a success.

    The caller's invariant is "either the old or the new complete file is
    present", and it already holds once ``os.replace`` returns. A directory
    ``fsync`` failure weakens only the durability of the rename across a power
    loss, so it is reported through the logger rather than as a failed write.
    """
    target = _seed(tmp_path / "vectors.json")
    monkeypatch.setattr(atomic_write, "_fsync_directory", _boom)

    atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == NEXT
    assert _temp_files(tmp_path) == []


def test_directory_fsync_is_skipped_when_the_platform_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot open a directory handle for ``fsync``; that is not an error."""
    target = tmp_path / "vectors.json"

    def unsupported(*_args: Any, **_kwargs: Any) -> int:
        raise OSError(errno.EACCES, "cannot open directory")

    monkeypatch.setattr(atomic_write, "_open_directory", unsupported)

    atomic_write_json(target, NEXT)

    assert json.loads(target.read_text(encoding="utf-8")) == NEXT


# --- Native/binary artifacts ---------------------------------------------


def test_atomic_replacement_publishes_on_clean_exit(tmp_path: Path) -> None:
    """A binary writer (FAISS, numpy) publishes through the same primitive."""
    target = tmp_path / "vectors.index"
    target.write_bytes(b"previous")

    with atomic_replacement(target) as staged:
        staged.write_bytes(b"next")

    assert target.read_bytes() == b"next"
    assert _temp_files(tmp_path) == []


def test_atomic_replacement_yields_a_same_directory_path(tmp_path: Path) -> None:
    """The staged path must be renameable onto the target without a copy."""
    target = tmp_path / "sub" / "vectors.index"
    target.parent.mkdir(parents=True)

    with atomic_replacement(target) as staged:
        assert staged.parent == target.parent
        assert staged.name.endswith(TEMP_SUFFIX)
        staged.write_bytes(b"next")


def test_atomic_replacement_discards_the_staged_file_on_failure(
    tmp_path: Path,
) -> None:
    """A writer that raises leaves the previous native artifact untouched."""
    target = tmp_path / "vectors.index"
    target.write_bytes(b"previous")

    with pytest.raises(RuntimeError):
        with atomic_replacement(target) as staged:
            staged.write_bytes(b"partial")
            raise RuntimeError("engine failed mid-write")

    assert target.read_bytes() == b"previous"
    assert _temp_files(tmp_path) == []


def test_atomic_replacement_requires_the_writer_to_produce_the_file(
    tmp_path: Path,
) -> None:
    """A writer that silently produced nothing must not delete the target."""
    target = tmp_path / "vectors.index"
    target.write_bytes(b"previous")

    with pytest.raises(FileNotFoundError):
        with atomic_replacement(target) as staged:
            staged.unlink()

    assert target.read_bytes() == b"previous"
    assert _temp_files(tmp_path) == []


# --- Orphaned temp cleanup ------------------------------------------------


def test_cleanup_removes_a_temp_file_left_by_a_dead_process(tmp_path: Path) -> None:
    """A crash between temp creation and replace leaves an orphan behind."""
    orphan = tmp_path / f".vectors.json.pid999999.abcd{TEMP_SUFFIX}"
    orphan.write_text("{partial", encoding="utf-8")

    removed = cleanup_orphaned_temp_files(tmp_path)

    assert removed == [orphan]
    assert not orphan.exists()


def test_cleanup_keeps_a_temp_file_owned_by_this_process(tmp_path: Path) -> None:
    """Another thread's in-flight write must not be deleted underneath it."""
    live = tmp_path / f".vectors.json.pid{os.getpid()}.abcd{TEMP_SUFFIX}"
    live.write_text("{partial", encoding="utf-8")

    removed = cleanup_orphaned_temp_files(tmp_path)

    assert removed == []
    assert live.exists()


def test_cleanup_never_touches_real_artifacts(tmp_path: Path) -> None:
    """Only this writer's own temp pattern is eligible for removal."""
    keep = _seed(tmp_path / "vectors.json")
    (tmp_path / "vectors.index").write_bytes(b"native")
    (tmp_path / "notes.tmp").write_text("unrelated", encoding="utf-8")

    removed = cleanup_orphaned_temp_files(tmp_path)

    assert removed == []
    assert json.loads(keep.read_text(encoding="utf-8")) == PREVIOUS
    assert (tmp_path / "vectors.index").exists()
    assert (tmp_path / "notes.tmp").exists()


def test_cleanup_of_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    """Startup runs before the index directory necessarily exists."""
    assert cleanup_orphaned_temp_files(tmp_path / "absent") == []


# --- Concurrency ----------------------------------------------------------


def test_concurrent_writers_never_publish_a_partial_file(tmp_path: Path) -> None:
    """Every reader observes one complete payload, whichever writer won."""
    target = tmp_path / "vectors.json"
    payloads = [{"writer": index, "count": index * 10} for index in range(8)]
    start = threading.Barrier(len(payloads))
    failures: list[BaseException] = []

    def write(payload: dict[str, Any]) -> None:
        try:
            start.wait(timeout=10)
            atomic_write_json(target, payload)
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert failures == []
    assert json.loads(target.read_text(encoding="utf-8")) in payloads
    assert _temp_files(tmp_path) == []
