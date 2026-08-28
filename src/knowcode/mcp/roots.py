"""Server root resolution and path containment for the MCP surface.

Two concerns live here, and both exist because the MCP server is registered
*once* and then spawned inside whatever repository the agent is working in.

**Resolution.** Claude Code does not document the working directory it spawns
a stdio server with, and its own guidance is to resolve project-relative
paths from ``CLAUDE_PROJECT_DIR`` rather than the process CWD. Relying on
CWD alone would make a single user-scope registration silently resolve
against the wrong tree, so the env var takes precedence over CWD while an
explicit ``--store`` still wins over both.

**Containment.** Once mutating actions (``build``, ``index``) are reachable
over MCP, an ``path`` argument becomes an instruction to parse a directory —
and, when an embedding provider is configured, to upload chunk text from it.
Every path an action accepts is therefore resolved against the server root
and rejected if it lands outside, so the blast radius of a tool call is the
repository the user actually pointed the server at.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping, Optional

from knowcode.errors import KnowCodePrerequisiteError

#: Environment variable Claude Code sets to the stable project root for
#: stdio MCP servers. Documented as the way to resolve project-relative
#: paths, precisely because the spawned CWD is not guaranteed.
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

logger = logging.getLogger(__name__)

#: Directory holding published generations, relative to the repository root.
INDEX_DIRNAME = "knowcode_index"


class PathOutsideRootError(KnowCodePrerequisiteError):
    """Raised when a requested path escapes the server root.

    Derives from :class:`KnowCodePrerequisiteError` so the MCP dispatcher's
    existing error projection reports it with a ``code`` and ``hint`` the
    agent can act on, rather than as an opaque failure.
    """

    def __init__(self, candidate: Path | str, root: Path) -> None:
        super().__init__(
            f"Path is outside the KnowCode server root: {candidate}",
            code="path_outside_root",
            hint=(
                f"This server only operates on {root}. Pass a path inside it, "
                "or start a separate KnowCode MCP server for the other "
                "repository."
            ),
        )
        self.candidate = str(candidate)
        self.root = root


def resolve_server_root(
    explicit: Optional[str | Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Resolve the repository root this server operates on.

    Precedence is explicit argument, then ``CLAUDE_PROJECT_DIR``, then the
    process CWD. A blank or stale env var is ignored rather than fatal: a
    leftover value from another project must not make the server unusable.

    Args:
        explicit: Value of ``--store``, if the user passed one.
        env: Environment mapping to read; defaults to ``os.environ``.

    Returns:
        An absolute, symlink-resolved directory path.

    Raises:
        FileNotFoundError: If an explicitly requested path does not exist.
            An explicit path is a user instruction, so a typo is surfaced
            instead of being silently replaced by a fallback.
    """
    environ = os.environ if env is None else env

    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"Store path does not exist: {candidate}")
        return candidate.resolve()

    from_env = (environ.get(PROJECT_DIR_ENV) or "").strip()
    if from_env:
        candidate = Path(from_env).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    return Path.cwd().resolve()


def store_is_ready(root: Path) -> bool:
    """Whether ``root`` has a readable knowledge store, in either layout.

    Two layouts are valid, and checking only the legacy one was a real defect:
    a current build publishes the graph as ``knowledge.db`` *inside* a
    generation and writes ``knowcode_knowledge.json`` only when
    ``export_json=True``. Gating on that JSON reported "store not found" for
    every repository built by a current KnowCode — even one with a fully
    published, searchable generation.

    Shared by the MCP server's per-action gate and the ``mcp-server`` startup
    banner so the two cannot disagree about the same repository.
    """
    from knowcode.storage.knowledge_store import KnowledgeStore

    store_file = root
    if store_file.is_dir():
        store_file = store_file / KnowledgeStore.DEFAULT_FILENAME
    if store_file.exists():
        return True

    from knowcode.indexing import generations

    try:
        resolved = generations.resolve_current_generation(root / INDEX_DIRNAME)
    except Exception as exc:  # noqa: BLE001 - an unreadable pointer is "not ready"
        # Logged because callers turn this into "run a build", which is the
        # wrong advice for a corrupted pointer. Keeping it silent made "not
        # built yet" and "artifacts are broken" indistinguishable.
        logger.warning("Treating %s as not ready: %s", root, exc)
        return False
    return resolved is not None and resolved.knowledge_db.exists()


def ensure_within_root(candidate: str | Path, root: Path) -> Path:
    """Resolve ``candidate`` against ``root`` and assert it stays inside.

    Relative paths are repository-relative, never CWD-relative, because the
    agent's notion of "." is the repository it is working in and the server's
    CWD is not guaranteed to match.

    Symlinks are followed before the containment check, so a link planted
    inside the root that points outside it is still an escape.

    Args:
        candidate: Path from a tool argument. May be relative or absolute.
        root: The server root, already resolved.

    Returns:
        The resolved absolute path.

    Raises:
        PathOutsideRootError: If the resolved path is neither the root nor a
            descendant of it.
    """
    root_resolved = root.resolve()
    raw = Path(candidate).expanduser()
    combined = raw if raw.is_absolute() else root_resolved / raw

    # ``strict=False`` so a not-yet-created output directory still resolves;
    # containment is a decision about the path, not about its existence.
    resolved = combined.resolve()

    if not _is_within(resolved, root_resolved):
        raise PathOutsideRootError(candidate, root_resolved)

    return resolved


def _is_within(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is ``root`` or below it.

    The component check is the authority. It compares path *components* rather
    than string prefixes, so a sibling that merely shares a prefix
    (``/repo-backup`` against ``/repo``) is never treated as contained.

    It is not sufficient on its own, though: ``resolve()`` follows symlinks but
    does not canonicalize *case*, and macOS is case-insensitive by default — so
    ``/repo/sub`` under root ``/Repo`` is one real directory that the component
    check rejects. ``os.path.normcase`` cannot fix this because it is a no-op
    everywhere except Windows. Filesystem identity can: comparing ``st_dev`` and
    ``st_ino`` asks the OS whether two names are the same directory, which is
    true exactly when the platform says it is.

    Identity is only consulted to *admit* a path the component check rejected,
    and only for ancestors that exist, so it cannot widen containment beyond
    directories the OS itself equates with the root.
    """
    if candidate == root or root in candidate.parents:
        return True

    try:
        root_stat = root.stat()
    except OSError:
        return False

    for ancestor in (candidate, *candidate.parents):
        try:
            if os.path.samestat(ancestor.stat(), root_stat):
                return True
        except OSError:
            # A not-yet-created output directory simply has no identity to
            # compare; keep walking toward an ancestor that does.
            continue
    return False
