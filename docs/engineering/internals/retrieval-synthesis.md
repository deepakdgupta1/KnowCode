# Internals: Retrieval & Synthesis

The path from a natural-language query to a token-budgeted context bundle.
Product-level explanations of the same behavior (with trade-offs) are in
[business logic](../../product/business-logic.md); this page is the
maintainer's view. Entry points: `retrieval/orchestrator.py`
(`RetrievalOrchestrator`), `analysis/context_synthesizer.py`.

## Query classification

`llm/query_classifier.py` scores weighted regex patterns per task type
(`explain, debug, extend, review, locate, general`);
`confidence = min(1, score / (0.5 × max possible))`, boosted up to +30% by
the gap to the runner-up. `TASK_PROMPTS` holds per-type expert personas
used by `ask`.

## Retrieval routing

`RetrievalOrchestrator.retrieve()`:

1. **Quoted queries** (`"exact name"`) route to the exact-match engine
   (`exact_query_engine.py`).
2. Otherwise **semantic retrieval** through the hybrid index.
3. On semantic failure → **lexical fallback** with keyword extraction;
   errors surfaced, mode recorded (never a silent empty result).
4. Results deduplicated to `limit_entities` unique entities.
5. **Dependency expansion** (`completeness.py`): 1-hop callees pulled in,
   labeled `source="dependency"`, score 0.0 — completeness without
   polluting evidence ranking.
6. Per-entity token budget: `max(200, min(2000, max_tokens /
   limit_entities))`.

## Hybrid fusion

`retrieval/hybrid_index.py`:

- BM25 (sparse) and vector (dense) retrievers run in parallel, each
  contributing `limit × 3` candidates (`CANDIDATE_FACTOR = 3`).
- Fused by Reciprocal Rank Fusion with `RRF_K = 60`:

```
score += (1 − α) / (60 + sparse_rank + 1)   # α = hybrid_alpha, default 0.2
score +=   α   / (60 + dense_rank  + 1)
```

- Candidate IDs are deduplicated by best rank before fusion; a dense ID
  unresolvable in the chunk repository is skipped without consuming a
  result slot.
- `HybridIndex.__init__`'s own default for `alpha` is 0.2, matching the
  `AppConfig` default; the service still passes
  `alpha=self.app_config.hybrid_alpha` explicitly, so a config change never
  depends on the constructor default.

## Reranking

`retrieval/reranker.py` reranks the top `limit × reranker_top_k_multiplier`
(default 5) fused candidates with the VoyageAI cross-encoder
(`rerank-2.5`). Any provider error degrades to deterministic signal
scoring — multiplicative boosts: docstring ×1.2, modified < 7 days ×1.1,
query substring in content ×1.5, query == chunk kind ×2.0.

## Context synthesis

`analysis/context_synthesizer.py`:

- `TASK_TEMPLATES` per task type define section priority order and budget
  boosts (e.g., DEBUG: source ×2.0, callers ×1.5).
- Greedy packing in priority order under `min(max_tokens × boost,
  max_tokens)`; **raw source capped at 50% of budget** in task mode;
  relationship lists capped (10 callers/callees, 15 children, 200-char
  docstring previews).
- `verbosity` ladder: `minimal` (context + sufficiency + tokens +
  reduction summary) → `standard` (+ query/mode/truncation) → `verbose`
  (+ evidence) → `diagnostic` (tests only). `summarize=True` omits raw
  source entirely.
- **Sufficiency** (`_calculate_sufficiency`): position-weighted coverage
  of the template priority list (`weight = 1/(i+1)`), +0.2 source bonus,
  +0.1 docstring > 50 chars, ×0.5 when the bundle is under 100
  characters; normalized by max score.

## Local-vs-LLM routing

`llm/agent.py` (`smart_answer`) escalates: 1500 tokens/1 entity minimal →
3000/3 + deps → same at `standard` verbosity → LLM. Local answering
requires the task type in `local_answer_task_types` (force-cleared at
config load; only a SHA-pinned machine-verified artifact repopulates it —
see `routing_policy.py` and [retrieval evals](../testing.md)) *and*
sufficiency ≥ `max(sufficiency_threshold, routing_quality_floor)`. One
logical query = one telemetry `query` event across all attempts.

The LLM leg (`agent.answer`) iterates configured models with a
client-side `RateLimiter` (per-model RPM/RPD), failing over on provider
resource exhaustion. Prompts follow `llm/prompt_contract.py`: system
channel carries instructions; question + context travel as one
JSON-escaped untrusted-input envelope with per-field caps (context 120k
chars, question 8k chars, error echo 200 chars).
