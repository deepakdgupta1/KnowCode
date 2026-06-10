---
trigger: always_on
last_updated: 2026-06-07
---

Follow `docs/mcp-contract.md` for the canonical KnowCode MCP retrieval policy.
Use the current chat context first. When repository context is still needed,
call `retrieve_context_for_query` with `verbosity=minimal` and the smallest
budget that fits the task.

Do NOT use generic file-system or search tools (such as `view_file`, `grep_search`, or `list_dir`) for initial context gathering or codebase exploration. Only use generic file-system tools when you have identified a specific file to edit, or when MCP retrieval is insufficient (sufficiency_score < 0.8) and you need to inspect specific file details.

Default starting points:

- Locate or explain one symbol: `max_tokens=1500`, `limit_entities=1`, `expand_deps=false`.
- Debug a concrete failure: `max_tokens=2000`, `limit_entities=2`, `expand_deps=true`.
- Review or extend a feature area: `max_tokens=3000`, `limit_entities=2-3`, `expand_deps=true`.

If `context_text` is insufficient, escalate in this order: increase breadth while
staying in `minimal`, then use `standard` for implementation detail, then use
`verbose` for ranking evidence. Use `diagnostic` only for retrieval debugging.

Use the configured `config.sufficiency_threshold` from `aimodels.yaml` to decide
whether local context is sufficient (default: `0.8`). If the score is below threshold, recover
missing local context before falling back to a larger external prompt.

Use focused KnowCode tools only after the first retrieval call:

- `search_codebase`: find entities by known name or pattern.
- `get_entity_context`: fetch context for a specific known entity.
- `trace_calls`: inspect callers or callees for a specific entity.
