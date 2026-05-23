# KnowCode MCP Retrieval Contract

**Last Updated:** 2026-05-23

This is the canonical operating policy for agents that use the KnowCode MCP
server. Keep agent rules, setup guides, and prompts pointed here instead of
redefining thresholds or token budgets in multiple places.

## Goal

Agents should minimize expensive context generation by asking KnowCode for the
smallest useful repository context first, then escalating only when the reduced
context is not enough to answer safely.

## Readiness

Before relying on MCP retrieval for a repository:

```bash
uv run knowcode analyze . --output .
uv run knowcode doctor --store . --mcp
```

`doctor` should confirm that the knowledge store exists, the semantic index is
compatible with the configured embedding model, and the MCP server can list and
call tools.

## First Tool

Use `retrieve_context_for_query` whenever the current conversation does not
already contain enough repository context.

Default MCP arguments:

```json
{
  "query": "<user question>",
  "task_type": "auto",
  "max_tokens": 1500,
  "limit_entities": 1,
  "expand_deps": false,
  "verbosity": "minimal"
}
```

Use larger starting budgets only when the question clearly needs more breadth:

| Query type | `max_tokens` | `limit_entities` | `expand_deps` |
| --- | ---: | ---: | --- |
| Locate or explain one symbol | 1500 | 1 | false |
| Debug a concrete failure | 2000 | 2 | true |
| Review or extend a feature area | 3000 | 2-3 | true |
| Trace callers, callees, or impact | 2000 | 2 | true |

## Verbosity Ladder

`verbosity="minimal"` is the default for IDE agents. In minimal mode,
KnowCode summarizes context and omits raw source/evidence metadata where it can.

Escalate only when the returned `context_text` is not enough:

1. Keep `verbosity="minimal"` and raise `max_tokens` or `limit_entities` if the
   answer needs more breadth.
2. Use `verbosity="standard"` if implementation detail or raw source is missing.
3. Use `verbosity="verbose"` if ranking evidence or retrieved chunk provenance
   is needed.
4. Use `verbosity="diagnostic"` only for tests and debugging the retrieval
   system, not as an agent default.

## Local Answer Gate

The local-answer threshold is configured in `aimodels.yaml`:

```yaml
config:
  sufficiency_threshold: 0.8
```

Agents should use that configured value. The recommended starting value is
`0.8`; tune it later from eval or telemetry data, not by hard-coding competing
thresholds in agent prompts.

If `sufficiency_score >= sufficiency_threshold` and `context_text` is non-empty,
the agent may answer from the retrieved context without sending repository
source to an external LLM.

If the score is below threshold, the agent should first use the verbosity ladder
when the missing information is likely available locally. Only fall back to a
larger external LLM prompt after the local context has clearly failed or the
user explicitly asks for a broader synthesis.

## Other Tools

Prefer `retrieve_context_for_query` for natural-language questions. Use the
other MCP tools only for focused follow-up:

- `search_codebase`: find entities by known name or pattern.
- `get_entity_context`: fetch context for a specific entity after its ID is known.
- `trace_calls`: inspect callers or callees for a specific entity.

## Agent Rule Snippet

Use this compact rule in agent-specific config files:

```md
When repository context is needed, follow docs/mcp-contract.md.
Start with retrieve_context_for_query using verbosity=minimal and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured sufficiency_threshold from
aimodels.yaml to decide whether to answer from local context.
```
