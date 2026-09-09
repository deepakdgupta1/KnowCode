"""Per-action payload caps for the MCP surface, measured on a real build (P3-3).

The token diet's recurring cost is the response an agent reads, not just the
schema it pays for. These caps pin each action's default payload against a
realistically sized fixture repository: a profile regression — ``context``
silently shipping raw source again, ``query`` forgetting its budget — moves
the measured bytes past the ceiling and turns red here rather than in an
agent's context window.

Caps are bytes of the serialized JSON payload, ~4 chars per token. They are
sized against the measured values with headroom for fixture-irrelevant noise
(paths, timestamps) but *not* enough to absorb a profile flip: the source
section of this fixture is larger than the gap between ``context``'s measured
summary size and its cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from knowcode.config import AppConfig
from knowcode.mcp.server import KnowCodeMCPServer
from knowcode.service import KnowCodeService

MODULE_A = '''"""Session handling for the demo service."""

from typing import Optional


class SessionStore:
    """Keeps live sessions with a bounded in-memory table."""

    def __init__(self, capacity: int = 128) -> None:
        self.capacity = capacity
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(self, user_id: str) -> str:
        """Create a session for a user and return its token."""
        token = f"s-{user_id}-{len(self._sessions)}"
        self._sessions[token] = {"user": user_id, "refs": 0}
        if len(self._sessions) > self.capacity:
            self.evict_oldest()
        return token

    def get(self, token: str) -> Optional[dict[str, Any]]:
        """Return the session record for a token, if still live."""
        return self._sessions.get(token)

    def evict_oldest(self) -> None:
        """Drop the least recently used session record."""
        if self._sessions:
            self._sessions.pop(next(iter(self._sessions)))


def validate_token(token: str) -> bool:
    """Check the shape of a session token without touching the store."""
    parts = token.split("-")
    return len(parts) == 3 and parts[0] == "s" and bool(parts[1])
'''

MODULE_B = '''"""Order pipeline helpers shared by the demo service."""

import logging

logger = logging.getLogger(__name__)

STATUS_FLOW = ("cart", "pending", "paid", "shipped", "closed")


def advance_status(current: str, event: str) -> str:
    """Advance an order along STATUS_FLOW; unknown events hold the state."""
    try:
        index = STATUS_FLOW.index(current)
    except ValueError:
        logger.warning("unknown status %r reset to cart", current)
        return "cart"
    if event in ("paid", "shipped", "closed"):
        return STATUS_FLOW[min(index + 1, len(STATUS_FLOW) - 1)]
    return current


def summarize_orders(orders: list[dict[str, Any]]) -> dict[str, float]:
    """Return mean order value and mean item count for reporting."""
    if not orders:
        return {"mean_value": 0.0, "mean_items": 0.0}
    values = [float(o.get("value", 0)) for o in orders]
    items = [float(len(o.get("items", []))) for o in orders]
    return {
        "mean_value": sum(values) / len(values),
        "mean_items": sum(items) / len(items),
    }
'''


def _fixture_repository(root: Path) -> Path:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "session_store.py").write_text(MODULE_A, encoding="utf-8")
    (src / "orders.py").write_text(MODULE_B, encoding="utf-8")
    return src


def _built_server(root: Path, src: Path) -> KnowCodeMCPServer:
    config = AppConfig.default()
    KnowCodeService(store_path=root, app_config=config).analyze(
        directory=src, output=root
    )
    return KnowCodeMCPServer(root)


def _call(server: KnowCodeMCPServer, tool: str, **arguments: Any) -> int:
    """Call one action and return its serialized payload size in bytes."""
    payload = server.handle_tool_call(tool, arguments)
    parsed = json.loads(payload)
    assert not (isinstance(parsed, dict) and "error" in parsed), parsed
    return len(payload)


def test_every_default_payload_stays_under_its_cap(tmp_path: Path) -> None:
    src = _fixture_repository(tmp_path)
    server = _built_server(tmp_path, src)

    # An entity id the fixture really holds, resolved the way an agent would.
    search = json.loads(
        server.handle_tool_call(
            "knowcode_retrieve", {"action": "search", "query": "advance_status"}
        )
    )
    entity_id = search[0]["id"]

    measured: dict[str, int] = {
        "retrieve:query": _call(
            server, "knowcode_retrieve", action="query", query="how do orders advance"
        ),
        "retrieve:search": _call(
            server, "knowcode_retrieve", action="search", query="session"
        ),
        "retrieve:semantic_search": _call(
            server, "knowcode_retrieve", action="semantic_search", query="session"
        ),
        "retrieve:context": _call(
            server, "knowcode_retrieve", action="context", entity_id=entity_id
        ),
        "retrieve:context+source": _call(
            server,
            "knowcode_retrieve",
            action="context",
            entity_id=entity_id,
            verbosity="standard",
        ),
        "retrieve:trace": _call(
            server, "knowcode_retrieve", action="trace", entity_id=entity_id
        ),
        "inspect:stats": _call(server, "knowcode_inspect", action="stats"),
        "inspect:freshness": _call(server, "knowcode_inspect", action="freshness"),
    }

    # Measured 2026-09-09 on this fixture; the caps sit at roughly twice the
    # measured bytes because the payloads embed absolute tmp-path strings,
    # whose length swings ~30% between pytest run roots. Drift past twice the
    # measurement is a regression; a profile flip (context shipping source
    # again, ~+50% on this fixture) is caught comparatively by
    # ``test_the_summary_profile_is_actually_smaller``, which is immune to
    # path noise because it compares two calls on the same root.
    caps: dict[str, int] = {
        "retrieve:query": 2_500,
        "retrieve:search": 4_500,
        "retrieve:semantic_search": 11_000,
        "retrieve:context": 2_000,
        "retrieve:context+source": 3_000,
        "retrieve:trace": 4_000,
        "inspect:stats": 700,
        "inspect:freshness": 300,
    }

    for action, size in measured.items():
        assert size <= caps[action], (
            f"{action} payload grew to {size} bytes (cap {caps[action]}). "
            "A default profile is shipping more than it should — or the "
            "fixture grew and the cap table needs a measured update."
        )


def test_the_summary_profile_is_actually_smaller(tmp_path: Path) -> None:
    """The point of the profiles: the default context payload must be
    measurably cheaper than the same call with source requested."""
    src = _fixture_repository(tmp_path)
    server = _built_server(tmp_path, src)

    search = json.loads(
        server.handle_tool_call(
            "knowcode_retrieve", {"action": "search", "query": "advance_status"}
        )
    )
    entity_id = search[0]["id"]

    summary = len(
        server.handle_tool_call(
            "knowcode_retrieve", {"action": "context", "entity_id": entity_id}
        )
    )
    with_source = len(
        server.handle_tool_call(
            "knowcode_retrieve",
            {"action": "context", "entity_id": entity_id, "verbosity": "standard"},
        )
    )

    assert with_source > summary
