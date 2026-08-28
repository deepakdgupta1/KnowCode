"""Artifact-producing MCP actions: build, index, export.

These are the actions that make the "one registration, every repository"
setup self-healing: an agent dropped into a repository KnowCode has never
seen can build the store itself instead of asking the user to open a
terminal.

Every action here is submitted to the :mod:`knowcode.mcp.jobs` registry and
returns a job id immediately. Indexing costs one embedding round-trip per
file with no cross-file batching and no concurrency, so a cold build on a
large repository runs for many minutes — long enough to freeze an agent turn
and to risk the client's stdio idle timeout.

Paths are confined to the server root by :func:`ensure_within_root` before
any work starts: with an embedding provider configured, a build uploads
chunk text, so an unconstrained ``path`` argument would let a tool call
exfiltrate a directory the user never pointed the server at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from knowcode.mcp.jobs import Job, JobRegistry, ProgressReporter
from knowcode.mcp.roots import INDEX_DIRNAME, ensure_within_root

if TYPE_CHECKING:  # pragma: no cover
    from knowcode.service import KnowCodeService

__all__ = ["INDEX_DIRNAME", "submit_build", "submit_export", "submit_index"]


def _ignore_list(raw: Any) -> Optional[list[str]]:
    """Coerce the ``ignore`` argument, rejecting non-string members."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("'ignore' must be an array of strings")
    if not all(isinstance(item, str) for item in raw):
        raise ValueError("'ignore' must contain only strings")
    return list(raw)


def submit_build(
    *,
    jobs: JobRegistry,
    root: Path,
    make_service: Callable[[], "KnowCodeService"],
    path: str = ".",
    incremental: bool = False,
    temporal: bool = False,
    ignore: Any = None,
    on_done: Optional[Callable[[], None]] = None,
) -> Job:
    """Submit a full build: graph, semantic index, and atomic publish.

    Args:
        jobs: Registry that will own the job.
        root: Server root; the store is written here.
        make_service: Factory returning a service the job exclusively owns.
            Called inside the job so construction cost and failure land on the
            job, not on the tool call. The job closes it.
        path: Directory to analyze, confined to ``root``.
        incremental: Reuse the previous generation's embeddings where source
            is unchanged.
        temporal: Also analyze git history.
        ignore: Extra ignore patterns.
        on_done: Called when the job reaches a terminal state, so the caller can
            refresh caches the new generation supersedes.

    Returns:
        The submitted job's initial snapshot.
    """
    directory = ensure_within_root(path, root)
    ignores = _ignore_list(ignore)

    def body(progress: ProgressReporter) -> dict[str, Any]:
        # The job owns this service and closes it: it is deliberately not the
        # server's shared read instance, so nothing else may close it while
        # the publication step is still running.
        service = make_service()
        try:
            stats = service.analyze(
                directory=directory,
                output=root,
                ignore=ignores,
                temporal=temporal,
                incremental=incremental,
                on_progress=progress,
            )
        finally:
            service.close()
        return _build_summary(stats)

    return jobs.submit("build", body, on_done=on_done)


def submit_index(
    *,
    jobs: JobRegistry,
    root: Path,
    make_service: Callable[[], "KnowCodeService"],
    path: str = ".",
    incremental: bool = False,
    on_done: Optional[Callable[[], None]] = None,
) -> Job:
    """Submit a semantic-index rebuild for an existing store."""
    directory = ensure_within_root(path, root)
    index_path = root / INDEX_DIRNAME

    def body(progress: ProgressReporter) -> dict[str, Any]:
        service = make_service()
        try:
            result = service.build_generation(
                directory,
                index_path,
                incremental=incremental,
                on_progress=progress,
            )
        finally:
            service.close()
        return {
            "published": result.published,
            "generation_id": result.generation_id,
            "generation_kind": result.kind,
            "chunk_count": result.chunk_count,
            **({"error": result.error} if result.error else {}),
            **({"stage": result.stage} if result.stage else {}),
        }

    return jobs.submit("index", body, on_done=on_done)


def submit_export(
    *,
    jobs: JobRegistry,
    root: Path,
    make_service: Callable[[], "KnowCodeService"],
    output: Optional[str] = None,
    on_done: Optional[Callable[[], None]] = None,
) -> Job:
    """Submit a Markdown documentation export.

    Writes files, so it lives on the lifecycle tool rather than ``inspect``
    despite reading no more than the store does.
    """
    if not output:
        raise ValueError("export requires 'output'")
    output_dir = ensure_within_root(output, root)

    def body(_progress: ProgressReporter) -> dict[str, Any]:
        from knowcode.analysis.documentation_synthesizer import (
            DocumentationSynthesizer,
        )

        service = make_service()
        try:
            bundle = DocumentationSynthesizer(service.store).write(output_dir)
        finally:
            service.close()
        return {
            "output": str(output_dir),
            # +1 for the manifest ``write`` emits alongside the documents.
            "files_written": len(bundle.documents) + 1,
        }

    return jobs.submit("export", body, on_done=on_done)


def _build_summary(stats: dict[str, Any]) -> dict[str, Any]:
    """Project build stats to the fields an agent acts on.

    ``published`` is the only success signal: ``build_generation`` reports a
    classified failure stage while leaving the previous generation current, so
    a truthy entity count says nothing about whether retrieval improved.
    """
    summary: dict[str, Any] = {
        "published": bool(stats.get("published")),
        "total_entities": stats.get("total_entities"),
        "total_relationships": stats.get("total_relationships"),
        "indexed_chunks": stats.get("indexed_chunks"),
        "generation_id": stats.get("generation_id"),
        "generation_kind": stats.get("generation_kind"),
    }
    for optional in ("total_errors", "index_error", "index_error_stage"):
        value = stats.get(optional)
        if value:
            summary[optional] = value

    report = stats.get("preflight_report")
    if isinstance(report, dict):
        # The full report card is available via inspect action='quality'; only
        # the headline belongs in a build result.
        summary["preflight"] = {
            "overall_score": report.get("overall_score"),
            "overall_grade": report.get("overall_grade"),
        }

    if not summary["published"]:
        summary["hint"] = (
            "The build did not publish a generation. The previously published "
            "generation (if any) is still current. Check 'index_error' and "
            "'index_error_stage', then retry."
        )
    return summary
