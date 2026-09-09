"""The MCP surface applies the response profiles on every source-bearing action (P3-2).

``knowcode_retrieve action='context'`` used to synthesize with source
unconditionally: the one action an agent reaches for to *look at* an entity
paid the raw-code cost on every call. The profiles make it summary-first like
``query``, escalate on the same ``verbosity`` ladder, and keep the deprecated
flat tool on its old shape — the compatibility option promised one release of
unchanged behaviour, and that promise is pinned here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from knowcode.mcp.server import KnowCodeMCPServer


class _RecordingStore:
    """Resolves any target to one entity, like the mock stores beside this file."""

    def __init__(self) -> None:
        from knowcode.data_models import Entity, EntityKind, Location

        self._entity = Entity(
            id="e1",
            name="Foo",
            kind=EntityKind.FUNCTION,
            qualified_name="module.Foo",
            location=Location(file_path="foo.py", line_start=1, line_end=10),
        )

    def get_entity(self, entity_id: str) -> Any:
        return self._entity

    def search(self, pattern: str) -> list[Any]:
        return [self._entity]


class _RecordingService:
    """Records the summarize flag the context route hands to get_context."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.store = _RecordingStore()
        self.summarize_flags: list[Optional[bool]] = []

    def get_context(
        self,
        entity_id: str,
        max_tokens: int = 2000,
        task_type: Optional[Any] = None,
        summarize: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        self.summarize_flags.append(summarize)
        return {
            "entity_id": entity_id,
            "context_text": "CTX",
            "total_tokens": 10,
            "truncated": False,
            "included_entities": [entity_id],
            "sufficiency_score": 0.5,
            "task_type": getattr(task_type, "value", "general"),
        }


def _server(tmp_path: Path) -> tuple[KnowCodeMCPServer, _RecordingService]:
    server = KnowCodeMCPServer(tmp_path)
    service = _RecordingService(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: service  # type: ignore[assignment]
    return server, service


def _call(server: KnowCodeMCPServer, **arguments: Any) -> dict[str, Any]:
    payload = server.handle_tool_call(
        "knowcode_retrieve", {"action": "context", **arguments}
    )
    return json.loads(payload)


def test_context_defaults_to_the_summary_profile(tmp_path: Path) -> None:
    server, service = _server(tmp_path)

    result = _call(server, entity_id="e1")

    assert result["context_text"] == "CTX"
    assert service.summarize_flags == [True]


def test_context_escalates_to_source_on_verbosity(tmp_path: Path) -> None:
    server, service = _server(tmp_path)

    _call(server, entity_id="e1", verbosity="standard")

    assert service.summarize_flags == [False]


def test_context_keeps_source_for_debug_and_review(tmp_path: Path) -> None:
    """The task-type floor holds even against the default verbosity: minimal
    *is* the default, so if an explicit minimal could suppress source, no
    debug or review call would ever include it."""
    server, service = _server(tmp_path)

    _call(server, entity_id="e1", task_type="debug")
    _call(server, entity_id="e1", task_type="review")
    _call(server, entity_id="e1", task_type="debug", verbosity="minimal")

    assert service.summarize_flags == [False, False, False]


def test_legacy_get_entity_context_keeps_its_old_shape(tmp_path: Path) -> None:
    """The deprecated flat tool promised unchanged behaviour for one release."""
    server = KnowCodeMCPServer(tmp_path)
    service = _RecordingService(tmp_path)
    server._ensure_service = lambda allow_missing_store=False: service  # type: ignore[assignment]

    payload = server.handle_tool_call(
        "get_entity_context", {"entity_id": "e1", "task_type": "general"}
    )

    assert json.loads(payload)["context_text"] == "CTX"
    assert service.summarize_flags == [False]
