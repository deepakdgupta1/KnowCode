# MCP Token Overhead Reduction Strategies

This document outlines the sources of token overhead when using the KnowCode MCP server and provides five concrete strategies to reduce this overhead by approximately 90%.

## Where the Tokens Are Burned

The MCP approach can be token-expensive due to the following overhead sources:

| Overhead Source                                                   | Est. Tokens/Call | Notes                                                                                |
| ----------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------ |
| **4 tool schemas** injected into every LLM prompt                 | ~600             | IDE injects ALL tool definitions into the system prompt on every turn                |
| **`context_text`** (up to 6000 tokens of source code)             | ~3000-6000       | Full source code for 3 entities with callers/callees                                 |
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

The current defaults in the MCP server are `max_tokens=6000` and `limit_entities=3`. For most day-to-day queries, this is excessive.
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
