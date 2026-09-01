---
trigger: always_on
last_updated: 2026-08-16
---

Follow `docs/mcp-contract.md` for the canonical KnowCode MCP retrieval
policy — including the default starting budgets per query type, the
verbosity escalation ladder, and the configured `sufficiency_threshold`
(default 0.8). Do not restate or override those values here or in any other
agent config.

Use the current chat context first. When repository context is needed, follow docs/mcp-contract.md.
Start with `knowcode_retrieve` `action=query`, `verbosity=minimal`, and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured `sufficiency_threshold` from
`aimodels.yaml` to decide whether to answer from local context.

If artifacts are missing or stale, run `knowcode_lifecycle` `action=build` and poll
`knowcode_inspect` `action=job_status` until it succeeds before retrying.

Do NOT use generic file-system or search tools (such as `view_file`,
`grep_search`, or `list_dir`) for initial context gathering or codebase
exploration. Only use generic file-system tools when you have identified a
specific file to edit, or when MCP retrieval is insufficient
(sufficiency_score below the configured threshold) and you need to inspect
specific file details.

Use focused KnowCode actions only after the first retrieval call:

- `knowcode_retrieve` `action=search`: find entities by known name or pattern.
- `knowcode_retrieve` `action=context`: fetch context for a specific entity after its ID is known.
- `knowcode_retrieve` `action=trace`: inspect callers or callees.
