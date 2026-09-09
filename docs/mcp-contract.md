# KnowCode MCP Retrieval Contract

**Last Updated:** 2026-08-28

This is the canonical operating policy for agents that use the KnowCode MCP
server. Keep agent rules, setup guides, and prompts pointed here instead of
redefining thresholds or token budgets in multiple places.

## Goal

Agents should minimize expensive context generation by asking KnowCode for the
smallest useful repository context first, then escalating only when the reduced
context is not enough to answer safely.

## Tool Surface

Three consolidated tools, each selecting a capability with an `action`:

| Tool | Actions | Nature |
|---|---|---|
| `knowcode_retrieve` | `query`, `search`, `context`, `trace`, `semantic_search` | Read-only. The hot path. |
| `knowcode_lifecycle` | `build`, `index`, `export` | Writes artifacts. Confirmed per call. |
| `knowcode_inspect` | `job_status`, `doctor`, `freshness`, `quality`, `stats`, `preflight`, `history`, `telemetry` | Read-only. |

Tool schemas are injected into every LLM request, so schema size is a
recurring per-turn cost. Measured at ~4 chars/token: the previous five flat
tools cost ~650 tokens for 5 capabilities; this surface costs ~1,110 for 14;
one tool per capability would cost ~2,000. The ceiling is enforced by
`tests/unit/mcp/test_consolidated_surface.py`.

The split is by concern because client permissions are per-tool
(`mcp__knowcode__<tool>`): `knowcode_retrieve` can be allowlisted so ordinary
questions never prompt, while `knowcode_lifecycle` still asks.

Deliberately **not** exposed: telemetry deletion (irreversible), `server` /
`mcp-server` / `install` (host-level process control), and `ask` (pays a
second LLM — an agent should consume context via `query` instead).

The five original flat tools (`search_codebase`, `get_entity_context`,
`trace_calls`, `retrieve_context_for_query`, `assess_codebase_quality`) remain
available for one release behind `--legacy-tools`, off by default.

## Readiness

The server starts whether or not artifacts exist. A repository KnowCode has
never seen is bootstrapped through the surface itself:

```json
{"tool": "knowcode_lifecycle", "action": "build"}
```

That returns a `job_id` immediately and indexes in the background — indexing
costs one embedding round-trip per file with no cross-file batching, so a
cold build on a large repository runs for many minutes. Poll until terminal:

```json
{"tool": "knowcode_inspect", "action": "job_status", "job_id": "j-…"}
```

Never report a build as successful before `state` is `succeeded` and
`result.published` is `true`. A build that fails after parsing leaves the
previously published generation current, so a non-zero entity count does not
mean retrieval improved.

From a terminal, the equivalent remains:

```bash
uv run knowcode build .
uv run knowcode doctor --store . --mcp
```

Two store layouts are valid, and both count as ready: a published generation
carrying `knowledge.db`, or the legacy flat `knowcode_knowledge.json`. A
current build produces the former and writes the latter only with
`export_json`.

## First Tool

Use `knowcode_retrieve` with `action="query"` whenever the current
conversation does not already contain enough repository context.

Default MCP arguments:

```json
{
  "action": "query",
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

Retrieval never builds. If artifacts are missing it returns
`code="missing_knowledge_store"` with a hint naming the lifecycle call —
it will not silently spend minutes and embedding quota on your behalf.

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

## Other Actions

Prefer `action="query"` for natural-language questions. Use the others only
for focused follow-up:

- `search`: find entities by known name or pattern.
- `context`: fetch context for a specific entity after its ID is known.
- `trace`: inspect callers or callees. Accepts a bare name and resolves it;
  an unresolvable name returns `code="entity_not_found"` rather than an empty
  list that would read as "nothing calls this".
- `semantic_search`: raw ranked chunks, capped per chunk. Bypasses the
  sufficiency projection, so it is a follow-up, not a first choice.
- `knowcode_inspect action="quality"`: the persisted pre-flight report when
  you need to judge how much to trust local context.
- `knowcode_inspect action="freshness"`: whether artifacts lag the working
  tree. Check this before trusting context on an actively edited repository.

## Agent Rule Snippet

Use this compact rule in agent-specific config files:

```md
When repository context is needed, follow docs/mcp-contract.md.
Start with knowcode_retrieve action=query, verbosity=minimal, and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured sufficiency_threshold from
aimodels.yaml to decide whether to answer from local context.
If artifacts are missing or stale, run knowcode_lifecycle action=build and poll
knowcode_inspect action=job_status until it succeeds before retrying.
```

---

## Appendix: Token Overhead Reduction Strategies


This document outlines the sources of token overhead when using the KnowCode MCP server and provides five concrete strategies to reduce this overhead by approximately 90%.

## Where the Tokens Are Burned

The MCP approach can be token-expensive due to the following overhead sources:

| Overhead Source                                                   | Est. Tokens/Call | Notes                                                                                |
| ----------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------ |
| **5 tool schemas** injected into every LLM prompt                 | ~650             | IDE injects ALL tool definitions into the system prompt on every turn                |
| **`context_text`** (up to 4000 tokens of source code)             | ~2000-4000       | Full source code for 3 entities with callers/callees                                 |
| **`evidence` array** (up to 15 entries × 5 fields each)           | ~300-500         | `rank`, `chunk_id`, `entity_id`, `score`, `source` per chunk                         |
| **`selected_entities` metadata** (per-entity duplication)         | ~150-300         | Re-echoes `entity_id`, `task_type`, `total_tokens`, `truncated`, `sufficiency_score` |
| **Echoed fields** (`query`, `max_tokens`, `retrieval_mode`, etc.) | ~100-200         | The query text itself is echoed back                                                 |
| **`json.dumps(indent=2)` whitespace**                             | ~200-400         | Pretty-printing doubles the character count                                          |
| **Total per call**                                                | **~4,500–8,000** |                                                                                      |

## 5 Strategies to Cut Overhead by 90%

> **Implementation Status:** all five strategies have shipped (see Status section below).

### 1. Consolidate the Tool Surface — *Implemented* (~2.3x cost per capability)

The 5 original flat tools (`search_codebase`, `get_entity_context`, `trace_calls`,
`retrieve_context_for_query`, `assess_codebase_quality`) were each injected into
_every_ LLM request.

**Change (shipped):** the default surface is three concern-split tools with
`action` enums — `knowcode_retrieve`, `knowcode_lifecycle`, `knowcode_inspect`.
The split is by concern rather than one `knowcode` tool because client
permissions are per-tool, so retrieval can be allowlisted while builds stay
confirmed. The five flat tools remain behind `--legacy-tools`, off by default.

Consolidation shipped together with a capability expansion, so the recurring
schema cost went from ~650 tokens for 5 capabilities to ~1,110 for 14 rather
than down to ~200. Cost per capability improved ~2.3x; absolute per-turn cost
did not fall until Strategy 5 shipped. A ceiling test guards the schema at
~1,140 tokens under a 1,200 limit.

### 2. Strip the Response to Essentials — *Implemented* (~50% response savings)

The response from the query path returned 12 fields. The agent realistically only needs 2–3 of these fields to proceed:

- `context_text` (the actual content)
- `sufficiency_score` (the decision metric)
- `total_tokens` (for budget awareness)

**Recommendation:** Omit all other fields (`query` echo, `task_confidence`, `retrieval_mode`, `max_tokens`, `truncated`, `evidence[]`, `selected_entities[]`, `errors[]`) by default, or gate them behind a `verbose=true` flag.

### 3. Slash `max_tokens` and `limit_entities` — *Implemented* (~60% content savings)

The previous defaults in the MCP server were `max_tokens=6000` and `limit_entities=3` (now `max_tokens=4000` by default). For most day-to-day queries, this is excessive.
**Recommendation:** Update your agent rules (`.agent/rules/context.md`) to use tiered budgets:

- `max_tokens=1500, limit_entities=1` is sufficient for "locate" and "explain" queries.
- `max_tokens=2000, limit_entities=2` for "debug" queries.
- Only use `max_tokens=3000+` for broad "extend" or "review" queries.

### 4. Remove `indent=2` from `json.dumps` — *Implemented* (~20% whitespace savings)

Tool results in `src/knowcode/mcp/server.py` were originally serialized with
`json.dumps(result, indent=2)`, roughly doubling the character count of every
response.

**Change (shipped):** serialization now uses a condensed format, instantly
removing hundreds of unnecessary whitespace tokens:

```python
return json.dumps(result, separators=(',', ':'))
```

### 5. Return Summaries Instead of Source Code — *Implemented* (6.4x on the probe entity)

Full `source_code` used to be dumped into `context_text` on every call.

**Change (shipped):** response profiles are one definition,
`retrieval/response_profiles.py`, that every source-bearing retrieval consumer
answers to. Summary-first is the default on `query` *and* `context`; escalation
rides the existing `verbosity` ladder rather than a new schema enum, because the
schema is paid on every turn. `semantic_search` is the explicit source request
and stays raw. Measured on this repository, default `context` fell from 4,313 to
674 bytes on the probe entity.

#### The source-hungry task types {#source-hungry-task-types}

`debug` and `review` include raw source **even at `minimal` verbosity**. This is
a floor, not a default: `minimal` *is* the default, so a floor an explicit
default could cancel is no floor at all.

This set is exactly `{debug, review}`. It is the canonical statement of the
rule — `retrieval/response_profiles.py` implements it and
`tests/unit/retrieval/test_response_profiles.py` pins the membership against
this section, so widening the set is a deliberate contract change rather than a
quiet edit. Adding a task type here takes source away from every default
response of that type.

---

## Combined Impact Estimate

If all strategies are implemented, the token savings would be dramatic:

| Strategy                      | Est. Token Savings                               |
| ----------------------------- | ------------------------------------------------ |
| Single tool (vs 5 schemas)    | ~400 tokens saved per turn                       |
| Stripped response metadata    | ~800 tokens saved per call                       |
| Lower default token limits    | ~3000 tokens saved per call                      |
| Compact JSON formatting       | ~300 tokens saved per call                       |
| Summaries vs full source code | ~2000 tokens saved per call                      |
| **Overall Reduction**         | **~6,500 tokens → ~800 tokens (≈88% reduction)** |

---

## Status and Implemented Optimizations (v1.1)

The following optimizations have been fully implemented in the KnowCode codebase:

1. **Stripped Response Metadata (Strategy 2 — Implemented)**: The default `minimal` verbosity mode now returns only `context_text`, `sufficiency_score`, and `total_tokens`. All non-essential fields (such as query echo, task confidence, evidence lists, etc.) are excluded, saving ~800 tokens per call.
2. **Lowered default token limits (Strategy 3 — Implemented)**: The default `max_tokens` has been reduced from `6000` to `4000` across `RetrievalOrchestrator` and `KnowCodeMCPServer`.
3. **Compact JSON Formatting (Strategy 4 — Implemented)**: Responses in `server.py` are serialized using `json.dumps(result, separators=(',', ':'))`, eliminating unnecessary whitespace and saving ~300 tokens per call.

4. **Tool consolidation (Strategy 1 — Implemented)**: the default surface is
   `knowcode_retrieve`, `knowcode_lifecycle`, and `knowcode_inspect`; the five
   flat tools are available behind `--legacy-tools` for one release, off by
   default. A ceiling test guards the recurring schema cost.
5. **Summary-first responses (Strategy 5 — Implemented, 2026-09-09)**: default
   retrieval payloads are summaries unless the agent escalates `verbosity` or
   the task type is one of the
   [source-hungry task types](#source-hungry-task-types). Payload sizes are
   capped by regression test and recorded per action in local telemetry.
