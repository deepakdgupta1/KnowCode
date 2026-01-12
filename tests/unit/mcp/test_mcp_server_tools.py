"""Unit tests for MCP tool routing without requiring the mcp package."""

from __future__ import annotations

import json
from pathlib import Path

from knowcode.mcp.server import KnowCodeMCPServer


class DummyService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def retrieve_context_for_query(self, query: str, **kwargs):  # noqa: ANN001
        self.calls.append(("retrieve_context_for_query", {"query": query, **kwargs}))
        return {
            "query": query,
            "task_type": "explain",
            "task_confidence": 1.0,
            "retrieval_mode": "semantic",
            "context_text": "CTX",
            "total_tokens": 10,
            "max_tokens": kwargs.get("max_tokens", 6000),
            "truncated": False,
            "sufficiency_score": 0.9,
            "selected_entities": [{"entity_id": "e1"}],
            "evidence": [],
            "errors": [],
        }


def test_handle_tool_call_retrieve_context_for_query(tmp_path: Path) -> None:
    server = KnowCodeMCPServer(store_path=tmp_path)
    dummy = DummyService()
    server._ensure_service = lambda: dummy  # type: ignore[method-assign]

    payload = json.loads(
        server.handle_tool_call(
            "retrieve_context_for_query",
            {
                "query": "Explain Foo",
                "task_type": "auto",
                "max_tokens": 123,
                "limit_entities": 2,
                "expand_deps": True,
            },
        )
    )

    assert payload["query"] == "Explain Foo"
    assert dummy.calls and dummy.calls[0][0] == "retrieve_context_for_query"
