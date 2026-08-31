"""Content identity of the running knowcode package.

An MCP server installed from a local checkout via ``uvx`` keeps executing the
cached tool environment it was first built with, silently, for as long as the
checkout moves ahead (BL-32) — on this repository that served a pre-audit
perfect resolution rate over a graph the repository's own build scored 0.158.
A content fingerprint of the package source lets a store record which code
built it and lets any reader prove it is running that same code.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import knowcode

#: Per-root cache of (stat signature, fingerprint). Publishing stamps the
#: builder on every build and watch batch; re-hashing the package source each
#: time is measurable, while stat-ing it is not. The signature covers every
#: file's path, mtime, and size, so any edit still moves the stamp — the
#: cache buys speed, not blindness.
_FINGERPRINT_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def _source_entries(package_root: Path) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    for path in sorted(package_root.rglob("*.py")):
        stat = os.stat(path)
        entries.append(
            (path.relative_to(package_root).as_posix(), stat.st_mtime_ns, stat.st_size)
        )
    return tuple(entries)


def _running_root(package_root: Path | None) -> Path:
    return (
        package_root
        if package_root is not None
        else Path(knowcode.__file__).resolve().parent
    )


def package_code_fingerprint(package_root: Path | None = None) -> str:
    """Fingerprint the Python source of the running knowcode package.

    sha256 over every ``*.py`` file under the package, taken in sorted
    relative-path order, hashing the path, then the bytes. The fingerprint is
    content identity, not install identity: the same source in a checkout venv
    and in a uv tool environment fingerprints identically, and any source edit
    — or rename — moves it.

    Args:
        package_root: Directory to fingerprint. Defaults to the installed
            ``knowcode`` package; tests point it at a fixture tree.
    """
    root = _running_root(package_root)
    entries = _source_entries(root)
    cache_key = str(root)
    cached = _FINGERPRINT_CACHE.get(cache_key)
    if cached is not None and cached[0] == entries:
        return cached[1]

    digest = hashlib.sha256()
    for relative_path, _, _ in entries:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((root / relative_path).read_bytes())
        digest.update(b"\x00")
    fingerprint = f"sha256:{digest.hexdigest()}"
    _FINGERPRINT_CACHE[cache_key] = (entries, fingerprint)
    return fingerprint


def builder_metadata(package_root: Path | None = None) -> dict[str, Any]:
    """The manifest stamp naming the code that is about to build a store."""
    root = _running_root(package_root)
    return {
        "code_fingerprint": package_code_fingerprint(root),
        "code_files": len(_source_entries(root)),
    }
