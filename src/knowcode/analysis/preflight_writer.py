"""Persistence for pre-flight assessment reports.

Writes and loads ``preflight_report.json`` alongside generation artifacts
so that ``doctor``, MCP tools, and the CLI can retrieve persisted reports
without re-running the assessment.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REPORT_FILENAME = "preflight_report.json"


def write_preflight_report(
    report_dict: dict[str, Any],
    target_dir: Path,
) -> Path:
    """Write a serialized ``PreflightReport`` to ``target_dir``.

    Args:
        report_dict: The report as returned by ``PreflightReport.to_dict()``.
        target_dir: Directory to write the report into (typically the
            generation directory or the index root).

    Returns:
        Path to the written report file.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / REPORT_FILENAME
    report_path.write_text(
        json.dumps(report_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Pre-flight report written to %s", report_path)
    return report_path


def load_preflight_report(
    search_dir: Path,
) -> Optional[dict[str, Any]]:
    """Load a persisted pre-flight report from ``search_dir``.

    Searches for ``preflight_report.json`` in:
    1. The given directory itself.
    2. The current generation directory (if ``search_dir`` is the index root).

    Args:
        search_dir: Directory to search for the report.

    Returns:
        The deserialized report dictionary, or ``None`` if no report is found.
    """
    # Direct location
    direct = search_dir / REPORT_FILENAME
    if direct.exists():
        return _read_report(direct)

    # Try current generation pointer
    pointer_path = search_dir / "current_generation"
    if pointer_path.exists():
        try:
            gen_name = pointer_path.read_text(encoding="utf-8").strip()
            gen_dir = search_dir / "generations" / gen_name
            gen_report = gen_dir / REPORT_FILENAME
            if gen_report.exists():
                return _read_report(gen_report)
        except (OSError, ValueError) as exc:
            logger.debug("Could not read generation pointer: %s", exc)

    return None


def _read_report(path: Path) -> Optional[dict[str, Any]]:
    """Read and parse a report file, returning ``None`` on failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("Pre-flight report at %s is not a JSON object.", path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read pre-flight report at %s: %s", path, exc)
        return None
