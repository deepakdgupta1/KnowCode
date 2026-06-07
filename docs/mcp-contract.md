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

---

## Appendix: Token Overhead Reduction Strategies


This document outlines the sources of token overhead when using the KnowCode MCP server and provides five concrete strategies to reduce this overhead by approximately 90%.

## Where the Tokens Are Burned

The MCP approach can be token-expensive due to the following overhead sources:

| Overhead Source                                                   | Est. Tokens/Call | Notes                                                                                |
| ----------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------ |
| **4 tool schemas** injected into every LLM prompt                 | ~600             | IDE injects ALL tool definitions into the system prompt on every turn                |
| **`context_text`** (up to 4000 tokens of source code)             | ~2000-4000       | Full source code for 3 entities with callers/callees                                 |
| **`evidence` array** (up to 15 entries × 5 fields each)           | ~300-500         | `rank`, `chunk_id`, `entity_id`, `score`, `source` per chunk                         |
| **`selected_entities` metadata** (per-entity duplication)         | ~150-300         | Re-echoes `entity_id`, `task_type`, `total_tokens`, `truncated`, `sufficiency_score` |
| **Echoed fields** (`query`, `max_tokens`, `retrieval_mode`, etc.) | ~100-200         | The query text itself is echoed back                                                 |
| **`json.dumps(indent=2)` whitespace**                             | ~200-400         | Pretty-printing doubles the character count                                          |
| **Total per call**                                                | **~4,500–8,000** |                                                                                      |

## 5 Strategies to Cut Overhead by 90%

### 1. Consolidate to a Single Tool (~60% tool-schema savings)

Currently, 4 separate tool definitions (`search_codebase`, `get_entity_context`, `trace_calls`, `retrieve_context_for_query`) are exposed by the MCP server and injected into _every_ LLM request.
**Recommendation:** Merge these into a single `knowcode` tool with an `action` parameter. This cuts the tool-schema overhead from ~600 tokens down to ~200 tokens per request.

### 2. Strip the Response to Essentials (~50% response savings)

The response from `retrieve_context_for_query` returns 12 fields. The agent realistically only needs 2–3 of these fields to proceed:

- `context_text` (the actual content)
- `sufficiency_score` (the decision metric)
- `total_tokens` (for budget awareness)

**Recommendation:** Omit all other fields (`query` echo, `task_confidence`, `retrieval_mode`, `max_tokens`, `truncated`, `evidence[]`, `selected_entities[]`, `errors[]`) by default, or gate them behind a `verbose=true` flag.

### 3. Slash `max_tokens` and `limit_entities` (~60% content savings)

The previous defaults in the MCP server were `max_tokens=6000` and `limit_entities=3` (now `max_tokens=4000` by default). For most day-to-day queries, this is excessive.
**Recommendation:** Update your agent rules (`.agent/rules/context.md`) to use tiered budgets:

- `max_tokens=1500, limit_entities=1` is sufficient for "locate" and "explain" queries.
- `max_tokens=2000, limit_entities=2` for "debug" queries.
- Only use `max_tokens=3000+` for broad "extend" or "review" queries.

### 4. Remove `indent=2` from `json.dumps` (~20% whitespace savings)

In `src/knowcode/mcp/server.py` (around line 347), the tool result is formatted with indentation:

```python
return json.dumps(result, indent=2)
```

**Recommendation:** Change this to a condensed format to instantly remove hundreds of unnecessary whitespace tokens:

```python
return json.dumps(result, separators=(',', ':'))
```

### 5. Return Summaries Instead of Source Code (Biggest Potential Win)

Currently, full `source_code` is dumped into `context_text`.
**Recommendation:** Instead of returning the full body of every function and class, return a **pre-summarized** digest (e.g., signature + docstring + key relationships). Only include raw source code when explicitly requested via `task_type=debug` or `task_type=review`. This single change could cut `context_text` from ~3000 tokens to ~500 tokens for most exploratory queries.

---

## Combined Impact Estimate

If all strategies are implemented, the token savings would be dramatic:

| Strategy                      | Est. Token Savings                               |
| ----------------------------- | ------------------------------------------------ |
| Single tool (vs 4 schemas)    | ~400 tokens saved per turn                       |
| Stripped response metadata    | ~800 tokens saved per call                       |
| Lower default token limits    | ~3000 tokens saved per call                      |
| Compact JSON formatting       | ~300 tokens saved per call                       |
| Summaries vs full source code | ~2000 tokens saved per call                      |
| **Overall Reduction**         | **~6,500 tokens → ~800 tokens (≈88% reduction)** |

---

## Status and Implemented Optimizations (v1.1)

The following optimizations have been fully implemented in the KnowCode codebase:

1. **Stripped Response Metadata (Implemented)**: The default `minimal` verbosity mode now returns only `context_text`, `sufficiency_score`, and `total_tokens`. All non-essential fields (such as query echo, task confidence, evidence lists, etc.) are excluded, saving ~800 tokens per call.
2. **Lowered default token limits (Implemented)**: The default `max_tokens` has been reduced from `6000` to `4000` across `RetrievalOrchestrator` and `KnowCodeMCPServer`.
3. **Compact JSON Formatting (Implemented)**: Responses in `server.py` are serialized using `json.dumps(result, separators=(',', ':'))`, eliminating unnecessary whitespace and saving ~300 tokens per call.
