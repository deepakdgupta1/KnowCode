"""The MCP surface over a really-published generation.

This is the regression that matters most. Before this, the MCP server tested
readiness by looking for the flat ``knowcode_knowledge.json``, which a current
build does not write — the graph is published as ``knowledge.db`` inside a
generation. So a repository with a complete, searchable generation answered
every retrieval call with ``missing_knowledge_store``, and the only way to fix
it was to produce a legacy JSON export by hand.

These tests drive the real pipeline (scan, parse, graph, chunk, embed,
publish) through the MCP tool surface, so they fail if that gap reopens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowcode.mcp.jobs import JobRegistry
from knowcode.mcp.server import KnowCodeMCPServer


def _repository(root: Path) -> None:
    """A small tree with a real call edge to trace."""
    src = root / "src"
    src.mkdir(parents=True)
    (src / "calc.py").write_text(
        '"""Arithmetic helpers."""\n\n\n'
        "def add(a: int, b: int) -> int:\n"
        '    """Return the sum of two integers."""\n'
        "    return a + b\n\n\n"
        "def total(values: list[int]) -> int:\n"
        '    """Sum a list by repeatedly calling add."""\n'
        "    result = 0\n"
        "    for value in values:\n"
        "        result = add(result, value)\n"
        "    return result\n",
        encoding="utf-8",
    )


def _inline(fn: object) -> None:
    """Run the lifecycle job synchronously so the test is deterministic."""
    fn()  # type: ignore[operator]


@pytest.fixture
def built_repo(tmp_path: Path) -> tuple[KnowCodeMCPServer, dict]:
    """A repository built through the MCP lifecycle tool."""
    _repository(tmp_path)
    server = KnowCodeMCPServer(
        tmp_path,
        jobs=JobRegistry(executor=_inline, id_factory=lambda: "j-build"),
    )
    submitted = json.loads(
        server.handle_tool_call("knowcode_lifecycle", {"action": "build"})
    )
    status = json.loads(
        server.handle_tool_call(
            "knowcode_inspect", {"action": "job_status", "job_id": "j-build"}
        )
    )
    assert status["state"] == "succeeded", status
    assert status["result"]["published"] is True, status
    return server, submitted


def _call(server: KnowCodeMCPServer, tool: str, **arguments: object) -> dict:
    parsed = json.loads(server.handle_tool_call(tool, arguments))
    return parsed if isinstance(parsed, dict) else {"_list": parsed}


class TestBuildThroughMcp:
    def test_build_publishes_without_writing_the_legacy_json(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        """Pins the layout that broke readiness detection."""
        server, _ = built_repo

        assert not (server.root / "knowcode_knowledge.json").exists()
        assert (server.root / "knowcode_index" / "current.json").exists()
        assert server._store_is_ready() is True

    def test_build_reports_real_progress(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        """Progress comes from the indexing loop, not an estimate."""
        server, _ = built_repo

        status = _call(server, "knowcode_inspect", action="job_status")
        assert status["files_total"] >= 1
        assert status["files_done"] == status["files_total"]

    def test_submit_returns_before_the_result_is_known(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        _server, submitted = built_repo

        assert submitted["job_id"] == "j-build"
        assert "result" not in submitted
        assert "job_status" in submitted["hint"]


class TestRetrievalAfterBuild:
    def test_query_answers_from_the_published_generation(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        server, _ = built_repo

        result = _call(
            server,
            "knowcode_retrieve",
            action="query",
            query="how does total compute a sum",
        )

        assert "error" not in result
        assert result["context_text"]
        assert 0.0 <= result["sufficiency_score"] <= 1.0

    def test_search_finds_a_defined_function(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        server, _ = built_repo

        result = _call(server, "knowcode_retrieve", action="search", query="add")

        names = {entity["name"] for entity in result["_list"]}
        assert "add" in names

    def test_trace_resolves_a_bare_name_to_its_entity(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        """A bare name must not silently answer "nothing calls this"."""
        server, _ = built_repo

        result = _call(
            server,
            "knowcode_retrieve",
            action="trace",
            entity_id="add",
            direction="callers",
        )

        assert result["entity_id"].endswith("::add")
        callers = {entry["name"] for entry in result["results"]}
        assert "total" in callers

    def test_semantic_search_returns_populated_chunks(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        """Guards the projection: the engine yields chunks, not score wrappers."""
        server, _ = built_repo

        result = _call(
            server, "knowcode_retrieve", action="semantic_search", query="sum a list"
        )

        chunks = result["_list"]
        assert chunks
        assert all(chunk["chunk_id"] for chunk in chunks)
        assert any(chunk["text"] for chunk in chunks)
        # An embedding vector would be thousands of useless tokens.
        assert all("embedding" not in chunk for chunk in chunks)

    def test_inspect_reports_fresh_and_graded(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        server, _ = built_repo

        freshness = _call(server, "knowcode_inspect", action="freshness")
        quality = _call(server, "knowcode_inspect", action="quality")
        stats = _call(server, "knowcode_inspect", action="stats")

        assert freshness["is_stale"] is False
        assert quality["overall_grade"]
        assert stats["total_entities"] >= 2

    def test_history_without_temporal_data_explains_itself(
        self, built_repo: tuple[KnowCodeMCPServer, dict]
    ) -> None:
        server, _ = built_repo

        result = _call(server, "knowcode_inspect", action="history")

        assert result["entries"] == []
        assert "temporal" in result["hint"]


class TestJobSerialization:
    def test_a_second_build_while_one_runs_is_refused(self, tmp_path: Path) -> None:
        """Two concurrent builds would race on the same staging directory."""
        _repository(tmp_path)
        server = KnowCodeMCPServer(
            tmp_path,
            # Never executes, so the first job stays RUNNING.
            jobs=JobRegistry(executor=lambda fn: None),
        )

        first = _call(server, "knowcode_lifecycle", action="build")
        second = _call(server, "knowcode_lifecycle", action="build")

        assert first["state"] == "running"
        assert second["code"] == "job_already_running"
        assert first["job_id"] in second["hint"]
