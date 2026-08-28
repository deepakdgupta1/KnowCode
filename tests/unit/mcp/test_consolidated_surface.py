"""Contract tests for the consolidated three-tool MCP surface.

These pin the properties the surface exists to guarantee:

- the schema stays small, because it is injected on every turn;
- a retrieval action never triggers a build as a side effect;
- lifecycle actions return a job id instead of blocking;
- paths cannot escape the server root;
- the server is usable in a repository with no store yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from knowcode.mcp.jobs import JobRegistry
from knowcode.mcp.server import KnowCodeMCPServer
from knowcode.mcp.tools import (
    INSPECT_ACTIONS,
    LIFECYCLE_ACTIONS,
    RETRIEVE_ACTIONS,
    tool_definitions,
)


def _inline(fn: Any) -> None:
    fn()


def _server(tmp_path: Path, **kwargs: Any) -> KnowCodeMCPServer:
    return KnowCodeMCPServer(
        tmp_path, jobs=JobRegistry(executor=_inline, id_factory=lambda: "j-test")
    )


def _call(server: KnowCodeMCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    payload = server.handle_tool_call(tool, arguments)
    parsed = json.loads(payload)
    return parsed if isinstance(parsed, dict) else {"_list": parsed}


class TestSurfaceShape:
    def test_default_surface_is_three_tools(self) -> None:
        assert [t["name"] for t in tool_definitions()] == [
            "knowcode_retrieve",
            "knowcode_lifecycle",
            "knowcode_inspect",
        ]

    def test_every_tool_requires_only_action(self) -> None:
        """Per-action requirements are validated in code, not in the schema.

        Encoding them as JSON Schema conditionals would multiply schema size,
        and schema size is paid on every turn.
        """
        for tool in tool_definitions():
            assert tool["inputSchema"]["required"] == ["action"]

    def test_schema_budget_holds(self) -> None:
        """Guard the recurring per-turn schema cost against regression.

        Honest accounting, at a conservative 4-chars-per-token estimate:

        - old surface: 5 tools, 5 capabilities, ~650 tokens
        - this surface: 3 tools, 14 capabilities, ~1,110 tokens
        - flat parity: 14 tools, 14 capabilities, ~2,000 tokens

        So this is *not* a reduction against today's cost — it is 1.7x the
        schema for 2.8x the capabilities, and roughly half what one-tool-per-
        capability would cost. The ceiling is set just above the measured
        value so that adding a capability forces a deliberate decision about
        the cost rather than letting it drift.
        """
        serialized = json.dumps(tool_definitions(), separators=(",", ":"))
        estimated_tokens = len(serialized) / 4
        assert estimated_tokens < 1200, (
            f"surface grew to ~{estimated_tokens:.0f} tokens. Trim descriptions "
            "or justify raising the ceiling — this is paid on every turn."
        )

    def test_legacy_surface_is_not_advertised_by_default(self) -> None:
        """Advertising both surfaces would pay both schema costs every turn."""
        default = json.dumps(tool_definitions(include_legacy=False))
        both = json.dumps(tool_definitions(include_legacy=True))
        assert len(both) > len(default) * 1.5

    def test_read_tools_declare_read_only(self) -> None:
        by_name = {t["name"]: t for t in tool_definitions()}
        assert by_name["knowcode_retrieve"]["annotations"]["readOnlyHint"] is True
        assert by_name["knowcode_inspect"]["annotations"]["readOnlyHint"] is True

    def test_lifecycle_tool_requires_user_interaction(self) -> None:
        """A build spends embedding quota and uploads source; it must confirm."""
        from knowcode.mcp.tools import REQUIRES_USER_INTERACTION

        by_name = {t["name"]: t for t in tool_definitions()}
        lifecycle = by_name["knowcode_lifecycle"]
        assert lifecycle["_meta"][REQUIRES_USER_INTERACTION] is True
        assert lifecycle["annotations"]["readOnlyHint"] is False

    def test_job_polling_is_not_on_the_confirming_tool(self) -> None:
        """Polling must not inherit per-call confirmation.

        ``job_status`` lives on the read-only inspect tool so an agent can
        poll a 20-minute build without prompting the user on every poll.
        """
        assert "job_status" in INSPECT_ACTIONS
        assert "job_status" not in LIFECYCLE_ACTIONS

    def test_destructive_capabilities_are_absent(self) -> None:
        """Telemetry deletion and daemon control are deliberately excluded."""
        all_actions = set(RETRIEVE_ACTIONS + LIFECYCLE_ACTIONS + INSPECT_ACTIONS)
        for excluded in {
            "telemetry_clear",
            "clear",
            "server",
            "mcp_server",
            "install",
            "ask",
        }:
            assert excluded not in all_actions


class TestActionValidation:
    def test_missing_action_is_a_validation_error(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_retrieve")

        assert "error" in result
        assert "action" in result["error"]

    def test_unknown_action_lists_the_valid_ones(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_retrieve", action="teleport")

        assert "error" in result
        assert "teleport" in result["error"]
        for action in RETRIEVE_ACTIONS:
            assert action in result["error"]

    def test_unknown_tool_still_reports_clearly(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_frobnicate", action="query")

        assert "Unknown tool" in result["error"]

    def test_action_requiring_query_reports_which_field(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_retrieve", action="query")

        assert "query" in result["error"]

    def test_action_requiring_entity_id_reports_which_field(
        self, tmp_path: Path
    ) -> None:
        result = _call(_server(tmp_path), "knowcode_retrieve", action="trace")

        assert "entity_id" in result["error"]

    def test_blank_query_is_rejected(self, tmp_path: Path) -> None:
        result = _call(
            _server(tmp_path), "knowcode_retrieve", action="query", query="  "
        )

        assert "error" in result


class TestBootstrap:
    """The server must be usable before a store exists."""

    def test_retrieval_without_a_store_is_actionable_not_fatal(
        self, tmp_path: Path
    ) -> None:
        result = _call(
            _server(tmp_path), "knowcode_retrieve", action="query", query="anything"
        )

        assert result["code"] == "missing_knowledge_store"
        # The hint must name a tool call: the agent has no terminal.
        assert "knowcode_lifecycle" in result["hint"]

    def test_build_does_not_require_an_existing_store(self, tmp_path: Path) -> None:
        """Otherwise the bootstrap action would be unreachable."""
        (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
        server = _server(tmp_path)

        result = _call(server, "knowcode_lifecycle", action="build")

        assert result["job_id"] == "j-test"
        assert "job_status" in result["hint"]
        assert result.get("code") != "missing_knowledge_store"


class TestLifecycleDispatch:
    def test_build_returns_a_job_id_rather_than_blocking(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("def f():\n    return 1\n")

        result = _call(_server(tmp_path), "knowcode_lifecycle", action="build")

        assert result["action"] == "build"
        assert result["job_id"] == "j-test"
        assert "state" in result

    def test_build_hint_tells_the_agent_not_to_assume_success(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "mod.py").write_text("x = 1\n")

        result = _call(_server(tmp_path), "knowcode_lifecycle", action="build")

        assert "Do not report success" in result["hint"]

    def test_path_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "other"
        outside.mkdir()
        server = KnowCodeMCPServer(
            root, jobs=JobRegistry(executor=_inline, id_factory=lambda: "j-test")
        )

        result = _call(server, "knowcode_lifecycle", action="build", path=str(outside))

        assert result["code"] == "path_outside_root"

    def test_parent_traversal_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        server = KnowCodeMCPServer(
            root, jobs=JobRegistry(executor=_inline, id_factory=lambda: "j-test")
        )

        result = _call(server, "knowcode_lifecycle", action="build", path="../")

        assert result["code"] == "path_outside_root"

    def test_export_requires_an_output_directory(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_lifecycle", action="export")

        assert "output" in result["error"]

    def test_non_string_ignore_patterns_are_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("x = 1\n")

        result = _call(
            _server(tmp_path), "knowcode_lifecycle", action="build", ignore=[1, 2]
        )

        assert "error" in result
        assert "ignore" in result["error"]

    def test_unknown_lifecycle_action_is_rejected(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_lifecycle", action="destroy")

        assert "destroy" in result["error"]


class TestInspectDispatch:
    def test_job_status_without_any_job_says_so(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_inspect", action="job_status")

        assert result["code"] == "no_jobs"

    def test_job_status_for_an_unknown_id_is_explicit(self, tmp_path: Path) -> None:
        result = _call(
            _server(tmp_path), "knowcode_inspect", action="job_status", job_id="nope"
        )

        assert result["code"] == "unknown_job"

    def test_job_status_defaults_to_the_latest_job(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("x = 1\n")
        server = _server(tmp_path)
        _call(server, "knowcode_lifecycle", action="build")

        result = _call(server, "knowcode_inspect", action="job_status")

        assert result["job_id"] == "j-test"
        assert result["state"] in {"running", "succeeded", "failed"}

    def test_unknown_inspect_action_is_rejected(self, tmp_path: Path) -> None:
        result = _call(_server(tmp_path), "knowcode_inspect", action="divine")

        assert "divine" in result["error"]


class TestReadsNeverBuild:
    """A read must never spend minutes and embedding quota by accident."""

    def test_semantic_search_without_an_index_refuses(self, tmp_path: Path) -> None:
        store = tmp_path / "knowcode_knowledge.json"
        store.write_text('{"entities": {}, "relationships": []}')
        server = _server(tmp_path)

        result = _call(
            server, "knowcode_retrieve", action="semantic_search", query="anything"
        )

        # Either the store fails to load or the index guard fires; both are
        # typed prerequisite errors, and neither starts a build.
        assert "error" in result
        assert server._jobs.latest() is None

    def test_no_read_action_ever_submits_a_job(self, tmp_path: Path) -> None:
        server = _server(tmp_path)

        for action in RETRIEVE_ACTIONS:
            _call(
                server,
                "knowcode_retrieve",
                action=action,
                query="q",
                entity_id="e",
            )
        for action in INSPECT_ACTIONS:
            _call(server, "knowcode_inspect", action=action)

        assert server._jobs.latest() is None


class TestTelemetry:
    def test_action_is_recorded_alongside_the_tool_name(self, tmp_path: Path) -> None:
        """Consolidation makes tool_name insufficient for per-capability counts."""
        server = _server(tmp_path)
        _call(server, "knowcode_inspect", action="job_status")

        log = tmp_path / "knowcode_telemetry.jsonl"
        events = [
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        ]
        tool_calls = [e for e in events if e.get("event_type") == "tool_call"]
        assert tool_calls
        assert tool_calls[-1]["tool_name"] == "knowcode_inspect"
        assert tool_calls[-1]["action"] == "job_status"

    def test_argument_values_are_never_logged(self, tmp_path: Path) -> None:
        server = _server(tmp_path)
        _call(
            server,
            "knowcode_retrieve",
            action="search",
            query="SuperSecretSymbolName",
        )

        log = tmp_path / "knowcode_telemetry.jsonl"
        contents = log.read_text()
        assert "SuperSecretSymbolName" not in contents
        assert "arguments" not in contents
