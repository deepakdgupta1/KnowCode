"""Where telemetry lives on disk, and the bounds it lives under (ADR 5).

Three properties are enforced here and nowhere else:

* **Location.** Telemetry belongs to the *store root*. It is never written into
  ``knowcode_index/`` — after Step 14 the resolved store path is often
  ``knowcode_index/generations/<id>/knowledge.db``, and a published generation
  is immutable and gets retired, which both corrupts the generation and hides
  the file from any deletion the user performs.
* **Bounds.** Files are created ``0600``, rotated at a size bound, kept to a
  fixed number of rotations, and swept by a retention window. "Append forever
  at whatever mode the umask gives us" was the previous policy.
* **Deletion.** One operation removes every artifact, including the
  correlation key, so deleting telemetry also rotates the identifier that ties
  records together.

Append-only files deliberately do not use :mod:`knowcode.utils.atomic_write`:
there is no previous version to preserve, and a torn final line is handled by
the reader, which skips records it cannot parse.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Iterator

TELEMETRY_FILENAME: Final = "knowcode_telemetry.jsonl"
RAW_TELEMETRY_FILENAME: Final = "knowcode_telemetry_raw.jsonl"
CORRELATION_KEY_FILENAME: Final = ".knowcode_telemetry_key"

#: Directory name that marks index artifacts. Telemetry never goes inside it.
INDEX_DIRNAME: Final = "knowcode_index"

#: Owner read/write only.
FILE_MODE: Final = 0o600

#: Default-file bounds. Roughly 5 MiB live plus three rotations.
MAX_FILE_BYTES: Final = 5 * 1024 * 1024
MAX_ROTATIONS: Final = 3
RETENTION_DAYS: Final = 30

#: Opt-in raw-capture bounds: smaller and shorter-lived by design.
RAW_MAX_FILE_BYTES: Final = 1024 * 1024
RAW_MAX_ROTATIONS: Final = 1
RAW_RETENTION_DAYS: Final = 7

#: A single record that exceeds this is dropped rather than written.
MAX_RECORD_CHARS: Final = 4096

#: Correlation-key material and the length of the id derived from it.
_KEY_BYTES: Final = 32
_QUERY_ID_CHARS: Final = 16

_key_cache: dict[Path, bytes] = {}
_key_lock = threading.Lock()


def resolve_store_root(store_path: str | Path | None) -> Path | None:
    """Return the directory telemetry for ``store_path`` belongs in.

    Accepts a store directory, a store file, or any path inside the index, and
    returns the store root. Returns ``None`` when no root can be derived, in
    which case the caller drops the event — telemetry is never written to the
    process working directory.
    """
    if store_path is None:
        return None
    path = Path(store_path)
    if not path.is_dir():
        path = path.parent
    # ``Path(".").parts`` is empty, so a relative store path — including the
    # CLI's own ``--store .`` default — has to be anchored before the index
    # check, or it would resolve to nothing and silently drop every event.
    if not path.is_absolute():
        path = Path.cwd() / path
    parts = path.parts
    if INDEX_DIRNAME in parts:
        path = Path(*parts[: parts.index(INDEX_DIRNAME)])
    if not path.parts:
        return None
    return path


def telemetry_path(store_path: str | Path) -> Path:
    """The default (aggregate-only) telemetry file for a store."""
    root = resolve_store_root(store_path)
    if root is None:  # pragma: no cover - callers resolve first
        raise ValueError("cannot resolve a store root for telemetry")
    return root / TELEMETRY_FILENAME


def raw_telemetry_path(store_path: str | Path) -> Path:
    """The opt-in raw-capture file for a store."""
    root = resolve_store_root(store_path)
    if root is None:  # pragma: no cover - callers resolve first
        raise ValueError("cannot resolve a store root for telemetry")
    return root / RAW_TELEMETRY_FILENAME


def correlation_key_path(store_path: str | Path) -> Path:
    """The per-store key file backing ``query_id``."""
    root = resolve_store_root(store_path)
    if root is None:  # pragma: no cover - callers resolve first
        raise ValueError("cannot resolve a store root for telemetry")
    return root / CORRELATION_KEY_FILENAME


def rotation_path(path: Path, index: int) -> Path:
    """The ``index``-th rotation of ``path`` (``…​.jsonl.1``)."""
    return path.with_name(f"{path.name}.{index}")


def _tighten(path: Path) -> None:
    """Force owner-only permissions, including on a file we did not create."""
    try:
        if (os.stat(path).st_mode & 0o777) != FILE_MODE:
            os.chmod(path, FILE_MODE)
    except OSError:  # pragma: no cover - racing deletion
        pass


def _sweep_retention(path: Path, retention_days: int) -> None:
    """Delete rotations older than the retention window."""
    cutoff = time.time() - retention_days * 86400
    for index in range(1, MAX_ROTATIONS + RAW_MAX_ROTATIONS + 2):
        rotation = rotation_path(path, index)
        if not rotation.exists():
            continue
        try:
            if rotation.stat().st_mtime < cutoff:
                rotation.unlink()
        except OSError:  # pragma: no cover - racing deletion
            pass


def _rotate(path: Path, max_rotations: int) -> None:
    """Shift ``…​.jsonl`` to ``…​.jsonl.1``, dropping the oldest rotation."""
    oldest = rotation_path(path, max_rotations)
    if oldest.exists():
        oldest.unlink()
    for index in range(max_rotations - 1, 0, -1):
        source = rotation_path(path, index)
        if source.exists():
            source.replace(rotation_path(path, index + 1))
    path.replace(rotation_path(path, 1))


def append_record(path: Path, line: str, *, raw: bool = False) -> None:
    """Append one JSONL record under the size, count, and retention bounds.

    The bounds are read from module attributes at call time so a deployment —
    or a test — can narrow them without re-importing callers.
    """
    max_bytes = RAW_MAX_FILE_BYTES if raw else MAX_FILE_BYTES
    max_rotations = RAW_MAX_ROTATIONS if raw else MAX_ROTATIONS
    retention_days = RAW_RETENTION_DAYS if raw else RETENTION_DAYS

    payload = line.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size + len(payload) > max_bytes:
            _rotate(path, max_rotations)
        else:
            _tighten(path)
    _sweep_retention(path, retention_days)

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    _tighten(path)


def existing_files(store_path: str | Path) -> list[Path]:
    """Every telemetry artifact that currently exists for a store."""
    root = resolve_store_root(store_path)
    if root is None:
        return []
    found: list[Path] = []
    for base in (root / TELEMETRY_FILENAME, root / RAW_TELEMETRY_FILENAME):
        if base.exists():
            found.append(base)
        found.extend(
            rotation
            for index in range(1, MAX_ROTATIONS + RAW_MAX_ROTATIONS + 2)
            if (rotation := rotation_path(base, index)).exists()
        )
    key = root / CORRELATION_KEY_FILENAME
    if key.exists():
        found.append(key)
    return found


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parseable records from ``path`` and its rotations, oldest first."""
    sources = [
        rotation_path(path, index)
        for index in range(MAX_ROTATIONS + RAW_MAX_ROTATIONS + 1, 0, -1)
    ]
    sources.append(path)
    for source in sources:
        if not source.exists():
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - racing deletion
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict):
                yield record


def delete_telemetry_files(store_path: str | Path) -> dict[str, Any]:
    """Remove every telemetry artifact for a store. Idempotent."""
    removed: list[str] = []
    for path in existing_files(store_path):
        try:
            path.unlink()
        except OSError:  # pragma: no cover - racing deletion
            continue
        removed.append(str(path))
    root = resolve_store_root(store_path)
    if root is not None:
        with _key_lock:
            _key_cache.pop(root, None)
    return {"removed": len(removed), "paths": removed}


def correlation_key(store_path: str | Path) -> bytes | None:
    """Return the per-store HMAC key, creating it on first use.

    The key never leaves the machine and is deleted with the telemetry it
    keys, so ``query_id`` is stable for trend review and meaningless to anyone
    holding only the log.
    """
    root = resolve_store_root(store_path)
    if root is None:
        return None
    with _key_lock:
        cached = _key_cache.get(root)
        if cached is not None:
            return cached
        path = root / CORRELATION_KEY_FILENAME
        try:
            if path.exists():
                material = path.read_bytes()
                _tighten(path)
            else:
                material = os.urandom(_KEY_BYTES)
                root.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
                try:
                    os.write(descriptor, material)
                finally:
                    os.close(descriptor)
                _tighten(path)
        except OSError:
            return None
        if not material:  # pragma: no cover - truncated key file
            return None
        _key_cache[root] = material
        return material


def query_id(store_path: str | Path, query: str) -> str:
    """Keyed, non-reversible correlation id for a query string."""
    key = correlation_key(store_path)
    if key is None:
        return ""
    digest = hmac.new(key, query.encode("utf-8", errors="replace"), sha256)
    return digest.hexdigest()[:_QUERY_ID_CHARS]
