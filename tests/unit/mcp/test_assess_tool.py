"""Unit tests for the assess_codebase_quality MCP tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from knowcode.mcp.server import KnowCodeMCPServer


class MockServiceWithReport:
    """Minimal mock service that returns a fixed generation with a report."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self._generation = _FakeGeneration(
            store_path / "knowcode_index" / "generations" / "gen_001"
        )

    def current_generation(self) -> Any:
        return self._generation

    @property
    def index_root(self) -> Path:
        return self.store_path / "knowcode_index"


class MockServiceNoReport:
    """Mock service with no pre-flight report available."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def current_generation(self) -> None:
        return None

    @property
    def index_root(self) -> Path:
        return self.store_path / "knowcode_index"


class _FakeGeneration:
    def __init__(self, path: Path) -> None:
        self.path = path


def test_assess_tool_with_persisted_report(tmp_path: Path) -> None:
    """When a pre-flight report exists, the tool returns it."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    # Create a fake generation directory with a report
    gen_dir = tmp_path / "knowcode_index" / "generations" / "gen_001"
    gen_dir.mkdir(parents=True)
    report = {
        "overall_score": 0.85,
        "overall_grade": "B",
        "dimensions": [],
        "summary": "Good codebase.",
        "recommendations": [],
    }
    (gen_dir / "preflight_report.json").write_text(json.dumps(report))

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock = MockServiceWithReport(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock  # type: ignore[assignment]

    result_str = server.handle_tool_call("assess_codebase_quality", {})
    result = json.loads(result_str)

    assert isinstance(result, dict)
    assert result["overall_score"] == 0.85
    assert result["overall_grade"] == "B"


def test_assess_tool_no_report(tmp_path: Path) -> None:
    """When no report exists, the tool returns an error dict."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock = MockServiceNoReport(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock  # type: ignore[assignment]

    result_str = server.handle_tool_call("assess_codebase_quality", {})
    result = json.loads(result_str)

    assert isinstance(result, dict)
    assert "error" in result
    assert "hint" in result


def test_quality_capability_is_reachable_on_the_default_surface() -> None:
    """The capability moved from a flat tool to an inspect action.

    The default surface no longer advertises ``assess_codebase_quality``: it
    is now ``knowcode_inspect`` with ``action='quality'``. Advertising a
    per-capability tool costs tokens on every turn, which is what the
    consolidated surface exists to avoid.
    """
    from knowcode.mcp.server import TOOL_DEFINITIONS

    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert "knowcode_inspect" in names
    assert "assess_codebase_quality" not in names

    inspect_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "knowcode_inspect")
    actions = inspect_tool["inputSchema"]["properties"]["action"]["enum"]
    assert "quality" in actions


def test_legacy_assess_tool_is_available_behind_the_opt_in() -> None:
    """The deprecated name stays reachable for one release."""
    from knowcode.mcp.tools import tool_definitions

    default_names = [t["name"] for t in tool_definitions(include_legacy=False)]
    legacy_names = [t["name"] for t in tool_definitions(include_legacy=True)]

    assert "assess_codebase_quality" not in default_names
    assert "assess_codebase_quality" in legacy_names
    # The consolidated surface is still advertised first when both are on.
    assert legacy_names[: len(default_names)] == default_names
