"""Crash-safe replacement for KnowCode's replace-style artifacts.

Step 13 of the hardening blueprint. Before it, ``KnowledgeStore.save``, both
vector metadata envelopes, and the index manifest each truncated their target
in place::

    with open(path, "w", encoding="utf-8") as f:   # target is now empty
        json.dump(payload, f)                      # a failure here loses it

Any failure — an unserializable payload, a full disk, a killed process — left
no readable artifact at all, so the previous valid generation was destroyed
before its replacement existed.

The contract (ADR 4, indexing invariant 7) is one sequence:

1. serialize **before** touching the filesystem, so an encoding failure cannot
   reach the target;
2. write into a temporary file in the **same directory**, so the later rename
   stays within one filesystem and is therefore atomic;
3. ``flush`` and ``fsync`` that file, so its bytes are durable before anything
   points at them;
4. ``os.replace`` onto the target, which is atomic for readers: they observe
   either the whole previous file or the whole new one;
5. ``fsync`` the parent directory where the platform supports it, so the rename
   itself survives power loss.

Any raised failure leaves the previous file byte-identical and removes the
temporary file. A directory ``fsync`` failure is logged rather than raised: by
then the replacement has already succeeded, so reporting it as a failed write
would be wrong.

One deliberate behavior change comes with the rename: where ``open(path, "w")``
followed a symlinked target and wrote through it, ``os.replace`` replaces the
symlink itself with a regular file. That is inherent to atomic replacement —
writing through the link would mean truncating the destination in place — and
no KnowCode artifact is published through a symlink.

**Publication ordering.** Data is published before the metadata that names it,
and a generation pointer last (Step 14 adds the pointer itself). Concretely:
each vector backend writes its native artifact before its ``vectors.json``
envelope, and :meth:`knowcode.indexing.indexer.Indexer.save` writes vectors and
chunks before ``index_manifest.json``. A crash therefore leaves at worst an
older manifest describing present data, never a manifest naming data that was
never written.

Append-only files (telemetry) deliberately do not use this module: they have no
previous version to preserve.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Suffix identifying this module's staging files. Kept distinctive so orphan
#: cleanup can never match an unrelated ``.tmp`` file a user left behind.
TEMP_SUFFIX = ".knowcode-tmp"

#: Explicit mode for published artifacts. ``mkstemp`` creates 0600 files, and
#: plain ``open(..., "w")`` is umask-dependent; index artifacts are neither.
DEFAULT_FILE_MODE = 0o644

# ``.<target name>.pid<pid>.<random>.knowcode-tmp``
_TEMP_NAME_PATTERN = re.compile(
    r"\.pid(?P<pid>\d+)\.[^.]+" + re.escape(TEMP_SUFFIX) + r"$"
)


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    encoding: str = "utf-8",
    mode: int = DEFAULT_FILE_MODE,
) -> Path:
    """Replace ``path`` with ``payload`` serialized as JSON, atomically.

    Args:
        path: Artifact to publish. Parent directories are created as needed.
        payload: Any JSON-serializable object.
        indent: ``json.dumps`` indent, for human-readable artifacts.
        sort_keys: Whether to sort object keys for byte-stable output.
        encoding: Explicit text encoding; never the locale default.
        mode: Explicit permission bits for the published file.

    Returns:
        The published path.

    Raises:
        TypeError: The payload is not JSON-serializable. Raised before any file
            is created, so the previous artifact is untouched.
        OSError: The staging write, sync, or rename failed. The previous
            artifact is untouched and the staging file is removed.
    """
    target = Path(path)
    # Serialize first: an unencodable payload must never reach the filesystem.
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys)

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = _make_temp(target)
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            _write_text(handle, text)
            _fsync_file(handle)
        os.chmod(staged, mode)
        _replace(staged, target)
    except BaseException:
        _discard(staged)
        raise

    _sync_parent(target.parent)
    return target


@contextmanager
def atomic_replacement(
    path: str | Path, *, mode: int = DEFAULT_FILE_MODE
) -> Iterator[Path]:
    """Yield a staging path that replaces ``path`` when the block exits cleanly.

    For artifacts written by a third-party writer that owns the file format —
    ``faiss.write_index`` and the numpy fallback's ``.npy`` — which would
    otherwise truncate the live native index in place.

    Raises:
        FileNotFoundError: The block exited without leaving a staged file. The
            target is left untouched rather than being deleted.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = _make_temp(target)
    os.close(descriptor)
    staged = Path(staged_name)

    try:
        yield staged
        if not staged.exists():
            raise FileNotFoundError(
                f"atomic replacement of {target} produced no staged file at {staged}"
            )
        _fsync_path(staged)
        os.chmod(staged, mode)
        _replace(staged, target)
    except BaseException:
        _discard(staged)
        raise

    _sync_parent(target.parent)


def cleanup_orphaned_temp_files(directory: str | Path) -> list[Path]:
    """Remove staging files abandoned by a crashed process.

    A process killed between staging and ``os.replace`` leaves its temporary
    file behind. Files staged by *this* process are skipped: another thread may
    be mid-write, and deleting its staging file underneath it would turn a
    successful write into a failed one.

    ADR 4 gives one writer process per artifact root, so a staging file from a
    different PID is by definition abandoned.

    Returns:
        The removed paths, sorted.
    """
    root = Path(directory)
    if not root.is_dir():
        return []

    current_pid = os.getpid()
    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        match = _TEMP_NAME_PATTERN.search(entry.name)
        if match is None or int(match.group("pid")) == current_pid:
            continue
        try:
            entry.unlink()
        except OSError as exc:  # pragma: no cover - platform/permission specific
            logger.debug("Could not remove orphaned temp file %s: %s", entry, exc)
            continue
        removed.append(entry)

    if removed:
        logger.info(
            "Removed %d orphaned artifact staging file(s) in %s", len(removed), root
        )
    return removed


def _make_temp(target: Path) -> tuple[int, str]:
    """Create a staging file beside ``target`` and return its fd and name."""
    return tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.pid{os.getpid()}.",
        suffix=TEMP_SUFFIX,
    )


def _discard(staged: Path) -> None:
    """Remove a staging file after a failed publication."""
    try:
        staged.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - platform/permission specific
        logger.debug("Could not remove staging file %s: %s", staged, exc)


# The four durability primitives are module-level so fault-injection tests can
# fail exactly one of them and assert the previous artifact survived.


def _write_text(handle: TextIO, text: str) -> None:
    """Write the serialized payload into the staging file."""
    handle.write(text)


def _fsync_file(handle: TextIO) -> None:
    """Flush Python buffers and force the staged bytes to the device."""
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_path(path: Path) -> None:
    """Force an externally written staged file to the device, best effort."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - platform specific
        logger.debug("Could not open %s to fsync it: %s", path, exc)
        return
    try:
        os.fsync(descriptor)
    except OSError as exc:  # pragma: no cover - platform specific
        logger.debug("Could not fsync %s: %s", path, exc)
    finally:
        os.close(descriptor)


def _replace(source: str | Path, destination: str | Path) -> None:
    """Atomically move the staged file onto the target."""
    os.replace(source, destination)


def _open_directory(directory: Path) -> int:
    """Open a directory for ``fsync``; unsupported on some platforms."""
    return os.open(directory, os.O_RDONLY)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself. Raises ``OSError`` if the platform refuses."""
    descriptor = _open_directory(directory)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_parent(directory: Path) -> None:
    """Sync the parent directory without ever failing a completed publication.

    Windows cannot open a directory handle this way, so the sync is skipped
    there; elsewhere a failure means the rename's durability across a power
    loss is weaker than intended. In both cases ``os.replace`` has already
    returned, so the caller's "the old file or the new file, never a partial
    one" invariant holds and raising here would misreport a successful write.
    """
    try:
        _fsync_directory(directory)
    except OSError as exc:
        logger.debug("Could not fsync %s after publishing: %s", directory, exc)
