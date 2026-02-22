"""Unit tests for gateway tool selection heuristics."""

from agent_gateway.tool_selector import select_tool_names



def test_select_tool_names_default_path_includes_query_and_context() -> None:
    selected = select_tool_names(
        user_message="Explain how caching works",
        allowed_tool_names=["query_context", "search", "get_context", "trace_calls"],
    )

    assert selected == ["query_context", "get_context"]



def test_select_tool_names_dependency_question_includes_trace() -> None:
    selected = select_tool_names(
        user_message="Who are the callers and dependencies of parser.build?",
        allowed_tool_names=["query_context", "search", "get_context", "trace_calls"],
    )

    assert "trace_calls" in selected



def test_select_tool_names_requested_list_is_filtered_and_deduped() -> None:
    selected = select_tool_names(
        user_message="ignored",
        allowed_tool_names=["query_context", "search"],
        requested_tool_names=["search", "search", "missing", "query_context"],
    )

    assert selected == ["search", "query_context"]
