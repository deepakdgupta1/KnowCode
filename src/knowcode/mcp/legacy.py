"""Dispatch for the deprecated flat MCP tools.

The five original tools — ``search_codebase``, ``get_entity_context``,
``trace_calls``, ``retrieve_context_for_query``, ``assess_codebase_quality`` —
are kept reachable for one release so existing client configurations and agent
rule files keep working while they migrate to the consolidated surface. They
are advertised only when the server is started with ``--legacy-tools``.

This lives in its own module because it is scheduled for deletion: when the
compatibility window closes, removing this file and its two call sites
removes the whole legacy surface. Each tool here maps onto exactly one
consolidated action, so there is no behaviour to port when it goes.
"""

from __future__ import annotations

from typing import Any, Final, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from knowcode.mcp.server import KnowCodeMCPServer


class _Unhandled:
    """Sentinel type for "this dispatcher does not know the tool"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNHANDLED"


#: Returned when the tool name is not a legacy tool. Distinct from ``None``,
#: which several tools could legitimately return as a payload.
UNHANDLED: Final = _Unhandled()

#: Legacy tool name -> the consolidated call that replaces it. Surfaced in
#: deprecation notices so a caller learns the migration from the response.
REPLACEMENTS: Final[dict[str, str]] = {
    "search_codebase": "knowcode_retrieve action='search'",
    "get_entity_context": "knowcode_retrieve action='context'",
    "trace_calls": "knowcode_retrieve action='trace'",
    "retrieve_context_for_query": "knowcode_retrieve action='query'",
    "assess_codebase_quality": "knowcode_inspect action='quality'",
}


def dispatch_legacy(
    server: "KnowCodeMCPServer", name: str, arguments: dict[str, Any]
) -> Any:
    """Route one deprecated flat tool call.

    Args:
        server: The server instance, for its capability methods.
        name: Tool name.
        arguments: Client-supplied arguments.

    Returns:
        The tool's result, or :data:`UNHANDLED` if ``name`` is not a legacy
        tool. Raises ``ValueError`` for invalid arguments, which the caller
        projects into a validation error.
    """
    if name == "search_codebase":
        if "query" not in arguments or not isinstance(arguments["query"], str):
            raise ValueError("search_codebase requires 'query' as a string")
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int):
            raise ValueError("'limit' must be an integer")
        return server.search_codebase(query=arguments["query"], limit=limit)

    if name == "get_entity_context":
        if "entity_id" not in arguments or not isinstance(arguments["entity_id"], str):
            raise ValueError("get_entity_context requires 'entity_id' as a string")
        return server.get_entity_context(
            entity_id=arguments["entity_id"],
            task_type=str(arguments.get("task_type", "general")),
            max_tokens=int(arguments.get("max_tokens", 2000)),
        )

    if name == "trace_calls":
        if "entity_id" not in arguments or not isinstance(arguments["entity_id"], str):
            raise ValueError("trace_calls requires 'entity_id' as a string")
        return server.trace_calls(
            entity_id=arguments["entity_id"],
            direction=str(arguments.get("direction", "callees")),
            depth=int(arguments.get("depth", 1)),
        )

    if name == "retrieve_context_for_query":
        if "query" not in arguments or not isinstance(arguments["query"], str):
            raise ValueError("retrieve_context_for_query requires 'query' as a string")
        return server.retrieve_context_for_query(
            query=arguments["query"],
            task_type=str(arguments.get("task_type", "auto")),
            max_tokens=int(arguments.get("max_tokens", 4000)),
            limit_entities=int(arguments.get("limit_entities", 3)),
            expand_deps=bool(arguments.get("expand_deps", True)),
            verbosity=str(arguments.get("verbosity", "minimal")),
        )

    if name == "assess_codebase_quality":
        return server.assess_codebase_quality()

    return UNHANDLED
