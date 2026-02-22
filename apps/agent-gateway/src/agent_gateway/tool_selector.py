"""Heuristic selection of tool names based on user intent."""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence



def select_tool_names(
    user_message: str,
    allowed_tool_names: Sequence[str],
    requested_tool_names: Optional[Iterable[str]] = None,
) -> List[str]:
    """Return a filtered set of tools for the given user intent."""

    allowed = {name for name in allowed_tool_names}
    if not allowed:
        return []

    if requested_tool_names is not None:
        selected = [name for name in requested_tool_names if name in allowed]
        return list(dict.fromkeys(selected))

    lower = user_message.lower()
    selected: List[str] = []

    def include(name: str) -> None:
        if name in allowed and name not in selected:
            selected.append(name)

    include("query_context")

    if any(word in lower for word in ("where", "find", "search", "locate", "symbol")):
        include("search")

    if any(
        word in lower
        for word in (
            "caller",
            "callee",
            "trace",
            "dependency",
            "impact",
            "downstream",
            "upstream",
        )
    ):
        include("trace_calls")

    if any(
        word in lower
        for word in (
            "context",
            "explain",
            "how",
            "why",
            "architecture",
            "flow",
        )
    ):
        include("get_context")

    # Keep a safe retrieval baseline for broad questions.
    if "get_context" not in selected and "get_context" in allowed and len(selected) == 1:
        include("get_context")

    return selected
