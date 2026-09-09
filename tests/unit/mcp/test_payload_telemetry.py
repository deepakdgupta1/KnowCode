"""Payload size is telemetry, not content (P3-3).

The roadmap asks for "payload-size distributions in local telemetry": the
recurring cost of the MCP surface is the response an agent reads, and a trend
line needs the size, not the bytes. ``payload_bytes`` is a length — the same
privacy posture as ``query_chars`` — and the summary aggregates it per tool
and action so a regression shows up as a moving percentile, not a feeling.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowcode.mcp.jobs import JobRegistry
from knowcode.mcp.server import KnowCodeMCPServer
from knowcode.telemetry import get_telemetry_summary


def _server(tmp_path: Path) -> KnowCodeMCPServer:
    return KnowCodeMCPServer(
        tmp_path, jobs=JobRegistry(executor=lambda fn: fn(), id_factory=lambda: "j-t")
    )


def _tool_call_events(tmp_path: Path) -> list[dict]:
    log = tmp_path / "knowcode_telemetry.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [e for e in events if e.get("event_type") == "tool_call"]


def test_a_tool_call_records_its_payload_size(tmp_path: Path) -> None:
    server = _server(tmp_path)

    payload = server.handle_tool_call("knowcode_inspect", {"action": "job_status"})

    events = _tool_call_events(tmp_path)
    assert events
    recorded = events[-1]
    assert recorded["payload_bytes"] == len(payload)


def test_payload_size_is_not_the_payload(tmp_path: Path) -> None:
    """The record carries a length, never the bytes it measured."""
    server = _server(tmp_path)
    server.handle_tool_call(
        "knowcode_retrieve", {"action": "search", "query": "SuperSecretSymbolName"}
    )

    log = (tmp_path / "knowcode_telemetry.jsonl").read_text()
    assert "SuperSecretSymbolName" not in log
    assert _tool_call_events(tmp_path)[-1]["payload_bytes"] > 0


def _write_tool_call(root: Path, tool: str, action: str, size: int) -> None:
    from knowcode.telemetry import log_event

    log_event(
        root,
        {
            "event_type": "tool_call",
            "tool_name": tool,
            "action": action,
            "argument_count": 1,
            "outcome": "ok",
            "payload_bytes": size,
        },
    )


def test_the_summary_reports_a_distribution_per_action(tmp_path: Path) -> None:
    for size in (100, 200, 300, 400, 500, 600, 700, 800):
        _write_tool_call(tmp_path, "knowcode_retrieve", "query", size)
    _write_tool_call(tmp_path, "knowcode_inspect", "stats", 50)

    summary = get_telemetry_summary(tmp_path)

    distribution = summary["payload_bytes_by_action"]
    assert set(distribution) == {
        "knowcode_retrieve:query",
        "knowcode_inspect:stats",
    }
    query = distribution["knowcode_retrieve:query"]
    assert query["count"] == 8
    assert query["max"] == 800
    # Nearest-rank percentiles name an observed payload, not an
    # interpolation: p50 of 8 samples is the 4th, p95 the 8th.
    assert query["p50"] == 400
    assert query["p95"] == 800
    assert distribution["knowcode_inspect:stats"]["max"] == 50


def test_records_without_a_size_are_not_distribution_input(tmp_path: Path) -> None:
    """Pre-existing records keep their old meaning; they simply do not
    contribute percentiles they never measured."""
    _write_tool_call(tmp_path, "knowcode_retrieve", "search", 120)
    from knowcode.telemetry import log_event

    log_event(
        tmp_path,
        {
            "event_type": "tool_call",
            "tool_name": "knowcode_retrieve",
            "action": "search",
            "argument_count": 1,
            "outcome": "ok",
        },
    )

    summary = get_telemetry_summary(tmp_path)

    assert summary["payload_bytes_by_action"]["knowcode_retrieve:search"] == {
        "count": 1,
        "p50": 120,
        "p95": 120,
        "max": 120,
    }


def test_an_empty_history_reports_an_empty_distribution(tmp_path: Path) -> None:
    summary = get_telemetry_summary(tmp_path)
    assert summary["payload_bytes_by_action"] == {}
