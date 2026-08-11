# ADR: KnowCode Persistence Format and Token Economics

**Status**: Proposed
**Date**: 2026-03-07
**Decision drivers**: Minimize frontier LLM token consumption by AI agents using KnowCode

---

## Context

`knowcode analyze` produces two artifact sets:

1. **Knowledge Store** (`knowcode_knowledge.json`, ~1.5 MB) — semantic graph of
   entities (functions, classes, modules) and relationships (calls, imports, contains,
   inherits). Entities carry embedded `source_code`, `docstring`, and `signature`.

2. **Semantic Index** (`knowcode_index/`, ~5.5 MB) — a separate retrieval index
   containing `chunks.json` (941 code chunks with content + BM25 tokens), a FAISS
   `vectors.index`, and a `vectors.json` ID map.

Source code is stored in **three places**: the knowledge store JSON, the chunk
repository, and the original source files on disk.

This ADR analyzes measured storage costs, traces the token flow from user query to
LLM response, and proposes phased improvements.

All numbers below are reproducible:
```bash
python scripts/measure_storage.py
```

---

## 1. Measured Storage Profile

### 1.1 Knowledge Store Breakdown

| Content Category    | Chars     | % of File | Purpose                        |
|---------------------|-----------|-----------|--------------------------------|
| **Source code**      | 571,942   | 38.2%     | Inline source for each entity  |
| **Relationships**    | 562,694   | 37.6%     | 2,698 edges (calls, imports…)  |
| Locations            | 59,734    | 4.0%      | file_path + line ranges        |
| IDs + names          | 47,178    | 3.1%      | Absolute-path entity IDs       |
| Docstrings           | 38,083    | 2.5%      | Entity documentation           |
| Signatures           | 21,703    | 1.4%      | Function/method signatures     |
| JSON structure       | 196,673   | 13.1%     | Keys, braces, indentation      |
| **Total**            | 1,498,007 | 100%      |                                |

### 1.2 Semantic Index Breakdown

| File              | Size       | Content                         |
|-------------------|------------|---------------------------------|
| chunks.json       | 1,676 KB   | 941 chunks with content + tokens |
| vectors.index     | 3,764 KB   | FAISS dense vector index        |
| vectors.json      | 89.5 KB    | Vector ID mappings              |
| **Total**         | **5,530 KB** |                               |

### 1.3 Source Code Duplication

| Location              | Chars    | Notes                             |
|-----------------------|----------|-----------------------------------|
| Knowledge store JSON  | 571,942  | 345 entities with source_code     |
| Chunk content         | 696,111  | 941 chunks (overlapping windows)  |
| Original .py files    | —        | Ground truth, authoritative       |

---

## 2. End-to-End Token Flow

### 2.1 Use Case

**IDE**: Google Antigravity (MCP-capable)
**Query**: "What authentication logic does this solution use?"
**Frontier LLM**: Gemini 2.5 Pro or Claude Sonnet (expensive, billed per token)
**Default tool**: `retrieve_context_for_query` at `verbosity="minimal"`

### 2.2 Critical Path Detail

The following sequence traces every component involved and marks where tokens are
consumed. Costs are categorized as:
- 🔴 **Frontier LLM** — most expensive (Gemini/Claude input/output pricing)
- 🟡 **External API** — moderate (VoyageAI embedding/reranking)
- 🟢 **Local** — free (CPU/RAM, no API call)

```
Step   Component                        Action                                           Cost
────   ─────────                        ──────                                           ────
 1     User → IDE                       Types question                                   —
 2     IDE → Frontier LLM               System prompt + tool schemas + history + msg     🔴 INPUT
 3     Frontier LLM → IDE               Emits tool call JSON                             🔴 OUTPUT
 4     IDE → MCP Server                 Forwards tool call via stdio                     —
 5     MCP Server                       _ensure_service() — creates KnowCodeService      🟢
                                         (cached after first call; NOT per-query)
 6     MCP Server                       KnowledgeStore.load() via service.store property  🟢
                                         (cached in self._store after first load)
 7     MCP Server                       classify_query() — regex task detection           🟢
 8     MCP Server → VoyageAI            embed_single(query) — query embedding            🟡
 9     MCP Server                       HybridIndex.search() — BM25 + FAISS              🟢
10     MCP Server → VoyageAI            Reranker.rerank() — cross-encoder on chunk       🟡
                                         content from chunks.json (NOT knowledge JSON)
11     MCP Server                       expand_dependencies() — graph walk                🟢
12     MCP Server                       ContextSynthesizer.synthesize_with_task()         🟢
                                         with summarize=True (minimal verbosity)
                                         → source_code is EXCLUDED from output
13     MCP Server → IDE                 Returns context bundle JSON                       —
14     IDE → Frontier LLM               Re-sends conversation + tool result               🔴 INPUT
15     Frontier LLM → IDE               Generates natural language answer                 🔴 OUTPUT
16     IDE → User                       Displays answer                                   —
```

### 2.3 What the Frontier LLM Actually Sees (per query)

The LLM processes tokens at steps 2, 3, 14, and 15. The breakdown varies by
verbosity mode and session length.

#### Step 2: Initial Request (IDE → LLM)

| Component                | Est. Tokens | Notes                                  |
|--------------------------|-------------|----------------------------------------|
| System prompt            | 500–1,500   | IDE persona, instructions              |
| MCP tool definitions     | 800–1,200   | 4 tool schemas, sent every turn        |
| Conversation history     | 0–50,000    | Compounds per turn                     |
| User message             | ~12         |                                        |
| IDE-injected context     | 500–5,000   | Open files, cursor, workspace metadata |
| **Sub-total**            | **1,800–57,000** | Dominated by history in long sessions |

> These costs are **independent of the JSON format**. Tool schema overhead and
> conversation history accumulation are IDE/protocol concerns, not storage concerns.

#### Step 3: Tool Call Decision (LLM → IDE)

| Component        | Est. Tokens | Notes                     |
|------------------|-------------|---------------------------|
| Tool name + args | 45–65       |                           |
| Reasoning        | 20–50       | Internal chain-of-thought |
| **Sub-total**    | **65–115**  | Output rate (3–5× input)  |

#### Step 14: Tool Result Injection (IDE → LLM)

This is the stage where storage format directly affects cost.

**At `verbosity="minimal"` (default MCP path):**

The orchestrator calls `get_context(..., summarize=True)` ([orchestrator.py](https://github.com/deepakdgupta1/KnowCode/blob/main/src/knowcode/retrieval/orchestrator.py#L176)).
The synthesizer **skips source code** when `summarize=True` ([context_synthesizer.py](https://github.com/deepakdgupta1/KnowCode/blob/main/src/knowcode/analysis/context_synthesizer.py#L142), [context_synthesizer.py](https://github.com/deepakdgupta1/KnowCode/blob/main/src/knowcode/analysis/context_synthesizer.py#L359)).

| Payload Component        | Per Entity | 3 Entities | Notes                    |
|--------------------------|------------|------------|--------------------------|
| Entity header            | ~40 tok    | ~120 tok   | Name, file, line range   |
| Docstring                | ~15–100    | ~45–300    |                          |
| Signature                | ~8–15      | ~24–45     |                          |
| Source code              | **0**      | **0**      | **Excluded in minimal**  |
| Parent context           | ~8–10      | ~24–30     |                          |
| Callers / callees lists  | ~25–50     | ~75–150    |                          |
| JSON envelope + metadata | ~10–20     | ~30–60     | sufficiency_score, etc.  |
| **Per-entity sub-total** | **~100–240** | **~300–700** |                       |

Measured simulation (3 representative entities): **~242 tokens**.

**At `verbosity="standard"` or `verbosity="verbose"`:**

The orchestrator calls `get_context(..., summarize=False)`. Source code IS included,
consuming up to 50% of the per-entity token budget.

| Payload Component        | Per Entity   | 3 Entities   | Notes                       |
|--------------------------|--------------|--------------|------------------------------|
| Header + docstring + sig | ~60–170      | ~180–510     | Same as above                |
| **Source code**           | **~200–1,000** | **~600–3,000** | **Dominant cost**        |
| Relations + metadata     | ~40–80       | ~120–240     |                              |
| **Per-entity sub-total** | **~300–1,250** | **~900–3,750** |                          |

Measured simulation (3 representative entities): **~1,236 tokens**.

**Summary: source code is 80% of the standard payload but 0% of the minimal payload.**

#### Step 15: Generated Answer (LLM → IDE)

| Component            | Est. Tokens |
|----------------------|-------------|
| Explanation prose    | 200–600     |
| Code snippets        | 100–400     |
| File references      | 30–60       |
| **Sub-total**        | **330–1,060** |

### 2.4 Frontier LLM Cost Per Query

| Verbosity | Input Tokens      | Output Tokens | Gemini 2.5 Pro | Claude Sonnet |
|-----------|-------------------|---------------|----------------|---------------|
| minimal   | 2,100–57,700      | 400–1,200     | $0.007–$0.07  | $0.012–$0.18 |
| standard  | 2,700–60,700      | 400–1,200     | $0.008–$0.08  | $0.014–$0.20 |
| verbose   | 3,000–62,000      | 400–1,200     | $0.009–$0.09  | $0.015–$0.21 |

> The dominant cost driver in long sessions is **conversation history accumulation**
> (up to 50K tokens), not the KnowCode payload. The JSON format primarily affects
> the ~300–3,700 token range occupied by the tool result.

### 2.5 VoyageAI API Costs (Step 8 + Step 10)

| Sub-step         | Tokens       | Notes                                       |
|------------------|--------------|---------------------------------------------|
| Query embedding  | ~5–10        | Single query string                         |
| Chunk reranking  | ~3,000–15,000 | 30 chunk contents from **chunks.json** (semantic index), NOT from the knowledge store JSON |

> Changing the knowledge store JSON will NOT reduce reranking costs. The reranker
> operates on chunk content from `knowcode_index/chunks.json`, which is a separate
> artifact produced by the indexer pipeline.

### 2.6 Local-First Answering: The Biggest Token Saver

The architecture already implements a **local-first answering** policy that can
skip the frontier LLM entirely:

```
agent.py:194
    if not force_llm and avg_sufficiency >= threshold and context_str:
        # Local-first: sufficient context found → zero frontier tokens
```

When the `sufficiency_score` ≥ 0.8 (configurable), the agent formats a local answer
from the context bundle without calling any external LLM. This saves the **entire
frontier LLM cost** for that query — both the tool result injection (Step 14) AND
the answer generation (Step 15).

The documented workflow from [reference_architecture.md](reference_architecture.md):
1. User prompts IDE agent
2. IDE agent invokes `retrieve_context_for_query` via MCP
3. KnowCode returns context bundle + sufficiency score
4. **If score ≥ 0.8: Agent answers locally (zero external tokens)**
5. If score < 0.8: Agent uses returned context with external LLM

**This is the single most impactful lever for minimizing frontier LLM token spend.**
Every query that resolves locally saves $0.01–$0.20.

---

## 3. Actual Issues with the Monolithic JSON

Scoped to problems **caused by the storage format**, not generic IDE/LLM overhead:

### 3.1 Source Code Stored but Not Used on Default Path

Source code is 38.2% of the JSON file (571,942 chars), but the default MCP retrieval
path (`verbosity="minimal"`) never includes it in the output. It is loaded into
memory, deserialized, and carried as `Entity.source_code` — then ignored by the
synthesizer when `summarize=True`.

**Impact**: Memory bloat (~572 KB of Python string objects), not direct token cost
on the default path. Source IS used when:
- `verbosity="standard"` or `"verbose"` is requested
- `get_entity_context` tool is called directly (uses `service.get_context`)
- `get_entity_details` is called (returns raw entity including source)
- The CLI `knowcode ask` pipeline runs with `summarize=False`

### 3.2 Startup Load Latency

The full JSON is deserialized once on first access via `KnowledgeStore.load()`. The
service caches the result in `self._store` ([service.py](https://github.com/deepakdgupta1/KnowCode/blob/main/src/knowcode/service.py#L48-L54)),
and the MCP server caches the service in `self._service` ([server.py](https://github.com/deepakdgupta1/KnowCode/blob/main/src/knowcode/mcp/server.py#L165-L182)).

**This is a startup cost, not a per-query cost.** For a long-lived MCP server, the
1.5 MB deserialization happens once. It becomes a problem when:
- The server restarts frequently (IDE restarts, reconnections)
- The codebase is large (10–100× KnowCode's size → 15–150 MB JSON)
- Multiple concurrent MCP sessions each instantiate their own service

### 3.3 Absolute-Path Entity IDs

Entity IDs use absolute paths (e.g., `/path/to/project/src/knowcode/data_models.py::EntityKind`), averaging 85 chars. With 397 entities referenced across 2,698 relationships, this contributes ~168K chars of redundant path prefixes.

**Impact**: File size bloat. Not a direct token cost (IDs are not sent to the LLM
in context bundles), but makes the JSON non-portable across machines.

### 3.4 No Partial-Load Capability

There is no way to load a subgraph of the knowledge store. Even if only 3 entities
are needed, all 397 entities and 2,698 relationships are deserialized. For this
codebase the cost is trivial (~50ms), but it scales linearly with codebase size.

### 3.5 Triple Source Storage

Source code exists in:
1. Knowledge store JSON: 571,942 chars
2. Chunk content: 696,111 chars
3. Original source files: ground truth

The knowledge store copy provides no unique value for the default retrieval path.
The chunk copy is required for BM25 search and reranking.

---

## 4. Options

### Option A: Keep Current Format, Optimize Output Contracts

**Change nothing in storage.** Focus on:
1. Maximizing the local-first answer hit rate (tune sufficiency threshold, improve
   scoring) — this is the single biggest token saver
2. Auditing non-minimal verbosity paths to ensure source code is only sent when
   the agent explicitly escalates
3. Verifying that IDE agents are configured to use `verbosity="minimal"` by default

**Pros**: No migration risk; no schema changes; improvements in token savings
are immediate and measurable.

**Cons**: Memory footprint remains inflated; startup latency unchanged; does not
address large-codebase scaling.

### Option B: Make `source_code` Optional in JSON (Source Sidecar)

Stop writing `source_code` into the main knowledge store. When non-minimal
synthesis needs source, resolve it lazily:

1. **Primary**: Read from the original source files using `Entity.location`
   (file_path + line_start + line_end)
2. **Fallback**: Read from `chunks.json` content (already indexed)

Requires:
- A `SourceResolver` component that reads source from disk on demand
- Updating `ContextSynthesizer` to call the resolver instead of `entity.source_code`
- Schema migration in `_migrate_schema()` to handle stores without source_code
- Updating `get_entity_details()` to use the resolver
- **No change to MCP tool schemas** (the external interface is unchanged)

**Does NOT require**: A host-side file-read tool exposed via MCP. The resolver
operates server-side within the KnowCode process, reading local files directly.

**Pros**: JSON shrinks ~40% (1.5 MB → 0.9 MB); memory footprint drops by ~572K;
source is always fresh from disk; chunk index is unaffected.

**Cons**: Source access adds disk I/O latency (~1ms/entity); breaks portability if
the knowledge store is moved away from the source tree; requires schema migration
support.

### Option C: Graph/Detail Split (Two-File Architecture)

Split the knowledge store into:

```
knowcode_graph.json    (~835 KB)   — Always loaded
  ├── Entity skeleton: IDs, names, kinds, signatures, locations
  └── All relationships

knowcode_details.json  (~90 KB)    — Loaded per-entity on demand
  ├── Docstrings
  └── Entity metadata
```

Source code: resolved from disk (as in Option B).

**Pros**: Graph topology always in memory for fast traversal; detail loaded lazily;
clear separation of concerns.

**Cons**: Two files to manage; graph file is still ~835 KB (dominated by relationship
data with absolute-path IDs); marginal benefit over Option B for this codebase size.

### Option D: SQLite-Backed Graph Store

Replace JSON with a SQLite database:

```sql
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    signature TEXT,
    docstring TEXT
    -- NO source_code column
);

CREATE TABLE relationships (
    source_id TEXT REFERENCES entities(id),
    target_id TEXT REFERENCES entities(id),
    kind TEXT NOT NULL
);
CREATE INDEX idx_rel_source ON relationships(source_id);
CREATE INDEX idx_rel_target ON relationships(target_id);
```

**Pros**: True partial loading (SQL queries return only needed data); indexed
relationship traversal; no in-memory graph needed; scales to very large codebases;
single file, transactional, portable.

**Cons**: Higher implementation effort; adds SQLite dependency; loses human-readable
storage; migration from JSON format needed.

---

## 5. Recommendation (Phased)

### Phase 1: Optimize Retrieval Contracts and Local-First Gating (Immediate)

**Rationale**: The biggest token savings come from avoiding frontier LLM calls
entirely, not from shrinking payloads by 500 tokens. The local-first mechanism
already exists — the priority is to maximize its hit rate.

Actions:
- [x] Measure current local-answer hit rate across representative queries
- [x] Tune sufficiency scoring weights and threshold (currently 0.8)
- [x] Ensure all MCP integration guides instruct IDEs to use `verbosity="minimal"`
- [ ] Audit `get_entity_context` callers to confirm they pass `summarize` correctly
- [x] Add telemetry: log `{query, sufficiency_score, source: "local"|"llm"}` per query

### Phase 2: Remove Source Code from Knowledge Store (After Measurement)

**Rationale**: If Phase 1 measurements show that non-minimal paths still consume
significant tokens and the 40% file size reduction improves startup latency, proceed
with Option B.

Actions:
- [ ] Implement `SourceResolver` that reads source via `Entity.location`
- [ ] Update `ContextSynthesizer` to use resolver instead of `entity.source_code`
- [ ] Update `get_entity_details()` to use resolver
- [ ] Bump `SCHEMA_VERSION` to 3; add migration in `_migrate_schema()`
- [ ] Keep `source_code` field in `_entity_to_dict()` as `None` (backwards compat)
- [ ] Update `measure_storage.py` to verify the reduction

### Phase 3: Consider SQLite If Startup Latency Justifies It (Large Repos Only)

**Rationale**: For codebases 10–100× larger, the JSON format will hit scaling limits
regardless of source_code removal. SQLite provides true partial loading.

Actions:
- [ ] Benchmark startup latency at 5K, 20K, 50K entities
- [ ] If >500ms at target codebase size, prototype SQLite backend
- [ ] Maintain JSON export for human inspection / migration

---

## 6. Key Corrections from Prior Analysis

| Prior Claim | Correction |
|-------------|------------|
| "Source code dominates minimal-verbosity payload" | Minimal verbosity sets `summarize=True`, which **excludes** source code. Source is 0% of minimal payload, 80% of standard. |
| "Every query loads the full JSON" | The store is cached in `service._store` and the service is cached in `server._service`. Load happens once at startup, not per-query. |
| "Removing source_code saves 85% of file" | Measured reduction is **40%** (1.5 MB → 0.9 MB). Relationships (37.6%) and JSON structure (13.1%) are the other major contributors. |
| "Reranking cost is downstream of JSON" | Reranking operates on chunk content from `knowcode_index/chunks.json`, a separate artifact. Changing the knowledge JSON does not reduce reranking cost. |
| "Remove source_code is a one-line change" | Requires a `SourceResolver`, schema migration, and updates to `ContextSynthesizer`, `get_entity_details()`, and non-minimal synthesis paths. |
| "Agent needs a Read tool to access source" | The MCP server reads files locally; no host-side tool exposure is needed. The resolver operates within the KnowCode process. |

---

## Appendix: Reproducing These Numbers

```bash
# Generate the measurement report
python scripts/measure_storage.py

# Machine-readable output
python scripts/measure_storage.py --json

# Against a different store
python scripts/measure_storage.py --store /path/to/knowcode_knowledge.json --index /path/to/knowcode_index
```
