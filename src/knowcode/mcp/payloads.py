"""Argument validation and response shaping for the MCP surface.

Both directions of the boundary between a client's JSON and KnowCode's own
objects live here: coercing untrusted tool arguments inward, and projecting
results outward at a size an agent can afford.

Kept out of ``server.py`` so the server module stays about dispatch, and
because every function here is pure and testable without a live store.
"""

from __future__ import annotations

import json
import logging
from typing import Any

#: Per-chunk text cap for ``semantic_search``. That action returns raw chunks
#: rather than a synthesized bundle, so an uncapped result set can dwarf the
#: budget the equivalent ``query`` would have spent.
CHUNK_TEXT_LIMIT = 800


def logger() -> logging.Logger:
    """Logger for the MCP package's server-side modules."""
    return logging.getLogger("knowcode.mcp.server")


def require_str(arguments: dict[str, Any], key: str, action: str) -> str:
    """Read a required string argument, naming the action that needs it.

    Per-action requirements are validated here rather than in the JSON schema:
    expressing "entity_id is required when action='trace'" as schema
    conditionals would multiply the schema size, and schema size is a cost paid
    on every turn.

    Raises:
        ValueError: If the value is missing, not a string, or blank.
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"action='{action}' requires '{key}' as a non-empty string")
    return value


def unknown_action(action: str, valid: list[str]) -> str:
    """Message naming the rejected action and the ones that would work."""
    return f"Unknown action: {action!r}. Valid actions: {', '.join(valid)}"


def chunk_to_dict(chunk: Any) -> dict[str, Any]:
    """Project a retrieved ``CodeChunk`` to a compact dict.

    The search engine returns chunks directly, with no score wrapper.
    ``embedding`` is deliberately dropped: it is a full float vector, worth
    thousands of tokens and of no use to an agent.
    """
    text = getattr(chunk, "content", None)
    truncated = False
    if isinstance(text, str) and len(text) > CHUNK_TEXT_LIMIT:
        text = text[:CHUNK_TEXT_LIMIT]
        truncated = True

    payload: dict[str, Any] = {
        "chunk_id": getattr(chunk, "id", None),
        "entity_id": getattr(chunk, "entity_id", None),
        "text": text,
    }
    if truncated:
        payload["truncated"] = True
    return payload


def is_error_payload(payload: str) -> bool:
    """Whether a serialized tool result represents a failure.

    Drives ``CallToolResult.isError`` so a client can tell a failed call from a
    successful one carrying an error-shaped body.
    """
    try:
        parsed = json.loads(payload)
    except ValueError:  # pragma: no cover - every branch emits JSON
        return True
    return isinstance(parsed, dict) and "error" in parsed
