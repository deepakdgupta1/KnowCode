# Sequence Diagram — Query / Retrieval Workflow

> Textual narration of [`seq_query_retrieval.drawio`](seq_query_retrieval.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode context` · `knowcode ask` · REST `POST /api/v1/context/query` · MCP `retrieve_context_for_query`

---

## Participants

| Participant | File | Role |
|---|---|---|
| User / Agent | — | Issues query or question |
| CLI / REST / MCP | `cli/cli.py`, `api/api.py`, `mcp/server.py` | Entry point — routes to KnowCodeService |
| KnowCodeService | `service.py` | Central orchestrator |
| RetrievalOrchestrator | `retrieval/orchestrator.py` | Validates, classifies, retrieves, synthesizes |
| query_classifier.py (Module) | llm/query_classifier.py | Detects task type via regex pattern matching |
| SearchEngine | `retrieval/search_engine.py` | Embeds query, calls HybridIndex, reranks |
| HybridIndex | `retrieval/hybrid_index.py` | Merges BM25 (lexical) + FAISS (dense) results |
| Reranker | `retrieval/reranker.py` | Cross-encoder reranking (VoyageAI primary, signal fallback) |
| expand\_dependencies | `retrieval/completeness.py` | Expands callee context for top-ranked chunks |
| ContextSynthesizer | `analysis/context_synthesizer.py` | Builds ContextBundle; computes sufficiency score |
| Agent / LLM (ask cmd) | `llm/agent.py` | Generates natural language answer (Alt B only) |

---

## Step 1 — User invokes query entry point

```
User → CLI/REST/MCP:  query / question / entity_id
```

The caller uses one of four entry points:
- `knowcode context <query>` — CLI, returns structured context
- `knowcode ask <question>` — CLI, returns LLM-generated answer
- `POST /api/v1/context/query` — REST API (`QueryRequest`)
- `retrieve_context_for_query` — MCP tool call

## Step 2 — Entry point calls service

```
CLI/REST/MCP → KnowCodeService:
  service.retrieve_context_for_query(
    query, max_tokens=6000, task_type,
    limit_entities=3, expand_deps, verbosity
  )
```

## Step 3 — Service delegates to orchestrator

```
KnowCodeService → RetrievalOrchestrator:
  orchestrator.retrieve_context_for_query(…)
```

## Step 4 — Validate preconditions

```
RetrievalOrchestrator:  _assert_store_exists()  +  _assert_index_exists()
```

Raises HTTP 412 if the knowledge store or semantic index has not been built yet.

## Step 5 — Classify query

```
RetrievalOrchestrator → query_classifier.py:  classify_query(query)
```

The classifier uses five sets of weighted regex patterns (one per `TaskType`):
- `EXPLAIN`, `DEBUG`, `EXTEND`, `REVIEW`, `LOCATE`

Returns: `(TaskType, confidence)`.

`resolved_task_type = task_type override (if caller supplied) OR detected task_type`

## Step 6 — Lazy-init search engine

```
RetrievalOrchestrator:
  service.get_search_engine()
  → HybridIndex(chunk_repository, vector_store)  [created once, cached]
```

## Step 7 — Validate index compatibility

```
RetrievalOrchestrator:
  _validate_index_compatibility(index_path)
  → checks embedding dimension + model name match
  → raises on mismatch
```

## Step 8 — Search: retrieve scored chunks

```
RetrievalOrchestrator → SearchEngine:
  engine.search_scored(query, limit=max(10, limit_entities×5), expand_deps)
```

### Step 9 — Embed query

```
SearchEngine:
  embedding_provider.embed_single(query)  →  query_vector  (dim=1024)
```

### Step 10 — Hybrid search

```
SearchEngine → HybridIndex:  hybrid_index.search(query, query_vec, limit=limit×2)
```

Internally HybridIndex executes three sub-steps:

- **10a** — BM25 search on `ChunkRepository` token lists (lexical)
- **10b** — FAISS similarity search on `VectorStore` (`IndexFlatIP`, cosine similarity via L2-normalized inner product)
- **10c** — Merge + normalize scores → `list[(CodeChunk, score)]`

Returns: top `limit×2` candidates back to SearchEngine.

### Step 11 — Rerank

```
SearchEngine → Reranker:  reranker.rerank(query, results, top_k=limit)
```

- **Primary**: VoyageAI `rerank-2.5` cross-encoder
- **Fallback** (if VoyageAI unavailable): signal-based scoring:
  - `boost_documented × 1.2`
  - `boost_recent × 1.1`
  - query text found in content: `× 1.5`
  - exact entity kind match: `× 2.0`

Returns: `list[(CodeChunk, score)]` top\_k reranked.

### Step 12 — Expand dependencies

```
SearchEngine → expand_dependencies(chunk, chunk_repo, store, max_depth=1)
```

For each top-ranked chunk (when `expand_deps=True`):
- `chunk_repo.get_by_entity(entity_id)` — fetch all chunks for the entity
- `store.get_callees(entity_id)` — walk CALLS relationships one level deep

Returns: `list[ScoredChunk]` with `source` field: `retrieved` (original result) or `dependency` (callee).

SearchEngine returns `List[ScoredChunk]` to RetrievalOrchestrator.

---

> **Note — Semantic fallback**: If semantic retrieval raises an exception,
> RetrievalOrchestrator falls back to lexical search:
> `store.search(query)` + keyword expansion.

---

## Step 13 — [Loop] Synthesize context per entity

For each selected `entity_id` (top `limit_entities` unique entities from the evidence list):

```
RetrievalOrchestrator → ContextSynthesizer:
  service.get_context(
    entity_id, task_type,
    per_entity_max_tokens,
    summarize=(verbosity == 'minimal')
  )
```

Internally:

- **13a** — `synthesize_with_task(entity_id, task_type)` — applies `TASK_TEMPLATES` priority order and per-section boost multipliers for the resolved task type
- **13b** — `_calculate_sufficiency(task_type, content_included, entity, text)` → float `0.0–1.0`

Returns:
```
{
  context_text,
  total_tokens,
  truncated,
  included_entities,
  task_type,
  sufficiency_score
}
```

## Step 14 — Assemble final response

```
RetrievalOrchestrator:
  context_text = '\n---\n'.join(context_parts)
  sufficiency  = avg(sufficiency_scores)
  apply verbosity filter
```

### Verbosity filter

| Level | Fields returned |
|---|---|
| `minimal` | `context_text`, `sufficiency_score`, `total_tokens`, `reduction_summary` |
| `standard` | + `query`, `task_type`, `task_confidence`, `retrieval_mode`, `max_tokens`, `truncated` |
| `verbose` | + `evidence[]` (`rank`, `chunk_id`, `entity_id`, `score`, `source`) |
| `diagnostic` | full dict — all fields + `errors[]` |

---

## Alt A — Return context to caller

**Applies to:** `CLI context` · `REST /api/v1/context/query` · `MCP retrieve_context_for_query`

```
Step 15a:
  KnowCodeService → CLI/REST/MCP:  QueryResponse / ContextResponse
  CLI/REST/MCP → User:             structured context dict
```

---

## Alt B — Ask command: pass to Agent / LLM

**Applies to:** `CLI ask`

### Step 15b — Invoke Agent

```
CLI → Agent:  agent.answer(query)  OR  agent.smart_answer(query, force_llm)
```

### smart\_answer sufficiency check

```
Agent:  check sufficiency_score ≥ threshold  (default 0.8, from AppConfig)
```

- **If sufficient**: `_format_local_answer()` — returns context-only answer; no LLM tokens consumed.
- **If insufficient or `force_llm=True`**: proceed to LLM call below.

### Step 16 — Build prompt

```
Agent:
  get_prompt_template(task_type)  +  context_text  +  question
```

### Step 17 — LLM failover loop

```
[ loop ]  for each model in config.models order (RPM + RPD rate-limit check per model)
```

- **17a** — Google Gemini: `client.models.generate_content(model, prompt)`
- **17b** — OpenAI-compatible (OpenRouter / Mistral): `client.chat.completions.create(model, messages)`
- **17c** — `rate_limiter.record_usage(model.name)` → `~/.knowcode/usage_stats.json`
- **17d** — On `ResourceExhausted` or other error → try next model in list

```
[ end loop ]
```

### Step 18 — Return answer

```
Agent → CLI:  answer text
```

### Step 19 — CLI returns to User

```
CLI → User:  {answer, source=llm|local, task_type, sufficiency_score}
```
