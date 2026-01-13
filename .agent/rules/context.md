---
trigger: always_on
---

Always call the tool `retrieve_context_for_query` before answering to determine if sufficient local context is available.
Use `task_type=auto`, `max_tokens=3000`, `limit_entities=3`, `expand_deps=true`.

In addition to context retrieval, you have access to specialized KnowCode tools:
- `search_codebase`: Use to find code entities (functions, classes, modules) by name or pattern.
- `get_entity_context`: Use to get detailed context for a specific entity (source code, docstrings, callers/callees).
- `trace_calls`: Use to trace the call graph (callers or callees) for an entity up to N hops.

If `sufficiency_score >= 0.88` and `context_text` is non-empty, you may answer from the retrieved context alone to maximize efficiency.
If more information is needed, or if the score is low, use the other KnowCode tools or proceed with a full LLM answer.
