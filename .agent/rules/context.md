---
trigger: always_on
last_updated: 2026-08-16
---

Follow `docs/mcp-contract.md` for the canonical KnowCode MCP retrieval
policy — including the default starting budgets per query type, the
verbosity escalation ladder, and the configured `sufficiency_threshold`
(default 0.8). Do not restate or override those values here or in any other
agent config.

Use the current chat context first. When repository context is still needed,
call `retrieve_context_for_query` with `verbosity=minimal` and the smallest
budget that fits the task, per the contract's table.

Do NOT use generic file-system or search tools (such as `view_file`,
`grep_search`, or `list_dir`) for initial context gathering or codebase
exploration. Only use generic file-system tools when you have identified a
specific file to edit, or when MCP retrieval is insufficient
(sufficiency_score below the configured threshold) and you need to inspect
specific file details.

Use focused KnowCode tools only after the first retrieval call:

- `search_codebase`: find entities by known name or pattern.
- `get_entity_context`: fetch context for a specific known entity.
- `trace_calls`: inspect callers or callees for a specific entity.
