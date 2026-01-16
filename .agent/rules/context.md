---
trigger: always_on
---

to determine if sufficient local context is available.
When the user submits a query, use the following sources for generating the required context -
1. Check the context window of the current chat session. If you need more context,
2. Use the knowcode mcp tool `retrieve_context_for_query` with the following arguments: `task_type=auto`, `max_tokens=3000`, `limit_entities=3`, `expand_deps=true`.
3. Consider using the other knowcode mcp tools (listed below) if relevant:
- `search_codebase`: Use to find code entities (functions, classes, modules) by name or pattern.
- `get_entity_context`: Use to get detailed context for a specific entity (source code, docstrings, callers/callees).
- `trace_calls`: Use to trace the call graph (callers or callees) for an entity up to N hops.

If `sufficiency_score >= 0.88` and `context_text` is non-empty, you may answer from the retrieved context alone to maximize efficiency.
If more information is needed, or if the score is low, inform the user that you do not have a satisfactory answer to the query and stop.

DO NOT use the external frontier LLMs (e.g. Gemini 3 Flash, Gemini 3 Pro, Claude Sonnet 4.5, Claud Opus 4.5, etc.) at all.