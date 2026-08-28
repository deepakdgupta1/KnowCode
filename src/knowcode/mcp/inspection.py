"""Read-only MCP inspection actions.

These answer "should I trust what KnowCode just told me, and is it current?"
— plus polling a lifecycle job. Job polling lives on the read-only inspect
tool rather than beside the actions that create jobs, because the lifecycle
tool carries a per-call user-confirmation annotation and an agent polling a
long build must not trigger a confirmation prompt on every poll.

Nothing here writes repository artifacts. Two rules keep it that way:

- No action may touch ``get_exact_query_engine`` or open an indexer on a
  missing index, both of which can trigger a *full build* as a side effect of
  what looks like a read.
- ``preflight`` parses the tree but persists nothing, which is what makes it
  safe to expose here despite its cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from knowcode.mcp.roots import ensure_within_root

if TYPE_CHECKING:  # pragma: no cover
    from knowcode.mcp.jobs import JobRegistry
    from knowcode.service import KnowCodeService

#: Cap on returned history rows regardless of the requested limit, so a
#: temporal repository cannot blow the agent's context on one call.
MAX_HISTORY_ROWS = 50


def job_status(jobs: "JobRegistry", job_id: Optional[str] = None) -> dict[str, Any]:
    """Report a lifecycle job's state.

    Args:
        jobs: The server's job registry.
        job_id: Job to report. Omitted means the most recent job, which is
            what an agent that just started a build wants.

    Returns:
        The job projection, or an error dict naming what is available.
    """
    job = jobs.get(job_id) if job_id else jobs.latest()
    if job is None:
        if job_id:
            return {
                "error": f"Unknown job_id: {job_id}",
                "code": "unknown_job",
                "hint": (
                    "Job state lives in the running MCP server process; ids "
                    "from an earlier session are gone. Start a new build."
                ),
            }
        return {
            "error": "No lifecycle job has been submitted in this session.",
            "code": "no_jobs",
            "hint": "Run knowcode_lifecycle action='build' first.",
        }
    return job.to_dict()


def doctor(root: Path, config_path: Optional[str] = None) -> dict[str, Any]:
    """Run the readiness check for this repository.

    ``include_mcp`` is deliberately ``False``: this is already running inside
    the MCP server, and the handshake check spawns a *second* server process,
    which would deadlock or double-load the store.
    """
    from knowcode.doctor import run_doctor

    report = run_doctor(
        store_path=str(root),
        index_path=None,
        config_path=config_path,
        include_mcp=False,
    )
    payload = report.to_dict()
    payload["mcp_handshake"] = "skipped_inside_server"
    return payload


def preflight(
    make_service: Callable[[], "KnowCodeService"],
    root: Path,
    path: str = ".",
) -> dict[str, Any]:
    """Run an ad-hoc quality assessment. Parses the tree; writes nothing."""
    directory = ensure_within_root(path, root)
    service = make_service()
    return service.preflight(directory)


def quality(make_service: Callable[[], "KnowCodeService"]) -> dict[str, Any]:
    """Return the pre-flight report card persisted by the last build."""
    from knowcode.analysis.preflight_writer import load_preflight_report

    service = make_service()
    generation = service.current_generation()
    if generation is not None:
        report = load_preflight_report(generation.path)
        if report is not None:
            return report

    report = load_preflight_report(service.index_root)
    if report is not None:
        return report

    return {
        "error": "No pre-flight report found.",
        "code": "missing_preflight_report",
        "hint": (
            "The assessment runs during a build. Run knowcode_lifecycle "
            "action='build', or knowcode_inspect action='preflight' for an "
            "ad-hoc assessment that persists nothing."
        ),
    }


def stats(make_service: Callable[[], "KnowCodeService"]) -> dict[str, Any]:
    """Return store counts.

    Chunk and vector counts appear only when the indexer is already open;
    ``get_stats`` deliberately does not open one, because doing so can create
    ``chunks.db`` as a side effect of a read.
    """
    return make_service().get_stats()


def freshness(make_service: Callable[[], "KnowCodeService"]) -> dict[str, Any]:
    """Report whether artifacts lag the working tree."""
    service = make_service()
    payload = service.get_freshness_metadata()
    if payload.get("is_stale"):
        payload["hint"] = (
            "Artifacts are behind the working tree. Retrieved context may "
            "describe stale code. Run knowcode_lifecycle action='build' "
            "(incremental=true is usually enough)."
        )
    return payload


def telemetry(root: Path) -> dict[str, Any]:
    """Return the local usage summary.

    Read-only, and never returns query text: the telemetry schema stores call
    shape, not content.
    """
    from knowcode.telemetry import get_telemetry_summary

    summary = get_telemetry_summary(str(root))
    return summary if isinstance(summary, dict) else {"summary": summary}


def history(
    make_service: Callable[[], "KnowCodeService"],
    target: Optional[str] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return commit history, or the revision history of one entity.

    Both shapes come from the service so this action does not re-derive the
    relationship traversal the CLI already implements.
    """
    service = make_service()
    capped = max(1, min(limit, MAX_HISTORY_ROWS))
    result = service.get_history(target=target, limit=capped)
    if not result.get("entries"):
        result["hint"] = (
            "No temporal data. History requires a build with temporal "
            "analysis enabled (knowcode_lifecycle action='build' "
            "temporal=true)."
        )
    return result
