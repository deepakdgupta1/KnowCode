"""Unit tests for MCP tool routing without requiring the mcp package."""

from __future__ import annotations

from typing import Any
import json
from pathlib import Path

import pytest

import knowcode.mcp.server as mcp_server_module
from knowcode.mcp.server import KnowCodeMCPServer
from knowcode.data_models import Entity, EntityKind, Location


class DummyService:
    """Dummy service for testing retrieve_context_for_query."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []  # type: ignore

    def retrieve_context_for_query(self, query: str, **kwargs: Any) -> Any:  # noqa: ANN001  # type: ignore
        self.calls.append(("retrieve_context_for_query", {"query": query, **kwargs}))
        return {
            "query": query,
            "task_type": "explain",
            "task_confidence": 1.0,
            "retrieval_mode": "semantic",
            "context_text": "CTX",
            "total_tokens": 10,
            "max_tokens": kwargs.get("max_tokens", 4000),
            "truncated": False,
            "sufficiency_score": 0.9,
            "selected_entities": [{"entity_id": "e1"}],
            "evidence": [],
            "errors": [],
        }


class MockStore:
    """Mock knowledge store for testing."""

    def __init__(self) -> None:
        self._entities = {
            "e1": Entity(
                id="e1",
                name="Foo",
                kind=EntityKind.FUNCTION,
                qualified_name="module.Foo",
                location=Location(file_path="foo.py", line_start=1, line_end=10),
            ),
            "e2": Entity(
                id="e2",
                name="Bar",
                kind=EntityKind.CLASS,
                qualified_name="module.Bar",
                location=Location(file_path="bar.py", line_start=5, line_end=50),
            ),
        }

    def search(self, query: str) -> list[Entity]:
        return [e for e in self._entities.values() if query.lower() in e.name.lower()]

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
        max_results: int = 50,
    ) -> list[dict]:  # type: ignore
        return [
            {
                "entity_id": "callee1",
                "qualified_name": "module.callee1",
                "kind": "function",
                "file": "callee.py",
                "line": 10,
                "call_depth": 1,
            }
        ]


class MockServiceWithStore:
    """Mock service with store for testing search_codebase and trace_calls."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = MockStore()
        self.store_path = tmp_path
        self.context_calls: list[tuple] = []  # type: ignore

    def get_context(
        self, target: str, max_tokens: int = 2000, task_type: Any = None
    ) -> Any:  # noqa: ANN001  # type: ignore
        self.context_calls.append((target, max_tokens, task_type))
        return {
            "entity_id": target,
            "context_text": f"def {target}(): pass",
            "total_tokens": 5,
            "truncated": False,
            "task_type": task_type.value if task_type else "general",
            "sufficiency_score": 0.85,
        }


def test_handle_tool_call_retrieve_context_for_query(tmp_path: Path) -> None:
    """Test retrieve_context_for_query tool routing."""
    server = KnowCodeMCPServer(store_path=tmp_path)
    dummy = DummyService()
    server._ensure_service = lambda allow_missing_store=False: dummy  # type: ignore

    payload = json.loads(
        server.handle_tool_call(
            "retrieve_context_for_query",
            {
                "query": "Explain Foo",
                "task_type": "auto",
                "max_tokens": 123,
                "limit_entities": 2,
                "expand_deps": True,
                "verbosity": "diagnostic",
            },
        )
    )

    assert payload["query"] == "Explain Foo"
    assert dummy.calls and dummy.calls[0][0] == "retrieve_context_for_query"
    assert dummy.calls[0][1]["verbosity"] == "diagnostic"


def test_handle_tool_call_search_codebase(tmp_path: Path) -> None:
    """Test search_codebase tool routing (UC2-001)."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock_service = MockServiceWithStore(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock_service  # type: ignore

    result = json.loads(
        server.handle_tool_call("search_codebase", {"query": "Foo", "limit": 5})
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "Foo"
    assert result[0]["kind"] == "function"
    assert result[0]["qualified_name"] == "module.Foo"
    assert result[0]["file"] == "foo.py"
    assert result[0]["line"] == 1


def test_handle_tool_call_get_entity_context(tmp_path: Path) -> None:
    """Test get_entity_context tool routing (UC2-002)."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock_service = MockServiceWithStore(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock_service  # type: ignore

    result = json.loads(
        server.handle_tool_call(
            "get_entity_context",
            {"entity_id": "e1", "task_type": "explain", "max_tokens": 1000},
        )
    )

    assert result["entity_id"] == "e1"
    assert result["sufficiency_score"] == 0.85
    assert result["task_type"] == "explain"
    assert result["qualified_name"] == "module.Foo"
    assert mock_service.context_calls  # Verify service was called


def test_handle_tool_call_trace_calls(tmp_path: Path) -> None:
    """Test trace_calls tool routing (UC2-003)."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock_service = MockServiceWithStore(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock_service  # type: ignore

    result = json.loads(
        server.handle_tool_call(
            "trace_calls",
            {"entity_id": "e1", "direction": "callees", "depth": 2},
        )
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["entity_id"] == "callee1"
    assert result[0]["call_depth"] == 1


def test_handle_tool_call_unknown_tool_returns_error(tmp_path: Path) -> None:
    """Test unknown tool returns error (UC2-004)."""
    server = KnowCodeMCPServer(store_path=tmp_path)

    result = json.loads(server.handle_tool_call("nonexistent_tool", {"foo": "bar"}))

    assert "error" in result
    assert "Unknown tool" in result["error"]


def test_handle_tool_call_reports_missing_prerequisites(tmp_path: Path) -> None:
    """Read tools should fail deterministically when store prerequisites are missing."""
    server = KnowCodeMCPServer(store_path=tmp_path)
    result = json.loads(server.handle_tool_call("search_codebase", {"query": "Foo"}))

    assert result["code"] == "missing_knowledge_store"
    assert "knowcode build" in result["hint"]


def test_mcp_server_initializes_service_with_strict_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP server should construct KnowCodeService with strict config validation."""
    (tmp_path / "knowcode_knowledge.json").write_text("{}")
    captured: dict[str, Any] = {}

    class DummyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(mcp_server_module, "KnowCodeService", DummyService)
    server = KnowCodeMCPServer(store_path=tmp_path)
    _ = server._ensure_service()

    assert captured.get("strict_config") is True


def test_retrieve_context_default_verbosity_minimal(tmp_path: Path) -> None:
    """Test retrieve_context_for_query defaults to minimal verbosity."""
    server = KnowCodeMCPServer(store_path=tmp_path)
    dummy = DummyService()
    server._ensure_service = lambda allow_missing_store=False: dummy  # type: ignore

    _ = json.loads(
        server.handle_tool_call(
            "retrieve_context_for_query",
            {
                "query": "Explain Foo",
            },
        )
    )

    assert dummy.calls[0][1]["verbosity"] == "minimal"


def test_retrieve_context_returns_expected_structure(tmp_path: Path) -> None:
    """Test retrieve_context_for_query returns the canonical structure."""
    server = KnowCodeMCPServer(store_path=tmp_path)
    dummy = DummyService()
    server._ensure_service = lambda allow_missing_store=False: dummy  # type: ignore

    payload = json.loads(
        server.handle_tool_call(
            "retrieve_context_for_query",
            {
                "query": "Explain Foo",
            },
        )
    )

    # Check that canonical keys are present in the response
    for key in [
        "query",
        "context_text",
        "sufficiency_score",
        "total_tokens",
        "task_type",
        "selected_entities",
    ]:
        assert key in payload


def test_handle_tool_call_emits_telemetry(tmp_path: Path) -> None:
    """handle_tool_call logs the call — without the arguments (Step 20).

    The argument payload is entirely client-supplied and routinely contains the
    user's question and pasted code. It used to be written to disk verbatim; it
    is now reduced to a count and, for query-bearing tools, the keyed
    correlation id.
    """
    # Write empty knowledge store so it starts but does not raise missing_knowledge_store
    (tmp_path / "knowcode_knowledge.json").write_text("{}")

    server = KnowCodeMCPServer(store_path=tmp_path)
    mock_service = MockServiceWithStore(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: mock_service  # type: ignore

    import time

    _ = server.handle_tool_call("search_codebase", {"query": "Foo", "limit": 5})

    # Wait for async logging to complete in a robust retry loop
    log_file = tmp_path / "knowcode_telemetry.jsonl"
    for _ in range(40):
        if log_file.exists():
            try:
                content = log_file.read_text(encoding="utf-8").strip()
                if content:
                    break
            except Exception:
                pass
        time.sleep(0.05)

    assert log_file.exists()

    import json

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines]

    tool_call_events = [r for r in records if r.get("event_type") == "tool_call"]
    assert len(tool_call_events) == 1
    assert tool_call_events[0]["tool_name"] == "search_codebase"
    assert tool_call_events[0]["argument_count"] == 2
    assert "arguments" not in tool_call_events[0]
    assert '"Foo"' not in log_file.read_text(encoding="utf-8")
