# Business Logic

Every user-visible heuristic in KnowCode, with its formula, defaults, and
the trade-off it encodes. Written for product decisions: when a stakeholder
asks "why does it behave that way?", the answer is here. Values were
verified against source; defaults live in `src/knowcode/config.py`,
`aimodels.yaml`, and the modules named per section.

## Indexing

### Chunking — entity-aligned, character-windowed

**What:** Each entity (function/class/method) becomes chunks of
`signature + docstring + source_code`, capped at **1000 characters with
100 characters of overlap** (`ChunkingConfig`, `data_models.py`). Modules
additionally contribute a header chunk and an imports chunk. Markdown,
reStructuredText, and YAML are parsed structurally (heading hierarchy /
config keys).

**Why:** Retrieval quality depends on chunks mapping to things developers
ask about; signature+docstring-first chunks make the most informative text
the most retrievable.

**Pros:** High precision for entity-centric questions; docstring text is
searchable. **Cons:** Character windows (not AST boundaries) can split
large constructs mid-expression; very large functions dilute their own
chunk.

### Incremental indexing and durable embeddings

**What:** Chunks unchanged since the last build (matched by content hash)
reuse their stored embeddings instead of re-calling the embedding API. A
failed watch-mode update is retryable and never partially applies; bulk
builds record per-file failures instead of aborting the generation.

**Pros:** Re-indexing after small edits costs almost nothing. **Cons:**
First build of a large repo pays full embedding cost.

### Atomic generations

**What:** Store + chunks + vectors publish all-or-nothing as a numbered
"generation"; readers hold leases so a running server hot-swaps to the new
generation without serving mixed state. A failed build leaves the previous
generation current.

**Pros:** Readers never observe a half-built index. **Cons:** Transient
2x artifact disk usage during publication.

## Retrieval

### Hybrid fusion — Reciprocal Rank Fusion, sparse-heavy

**What:** BM25 (keyword) and dense vector search run in parallel, each
contributing `CANDIDATE_FACTOR = 3` times the limit *they* are given — which
`SearchEngine` has already multiplied by `reranker_top_k_multiplier = 5`, so
each retriever sees 15× the result count the caller asked for. Fused by RRF:

```
score(chunk) = Σ_sparse (1 − α)/(60 + rank + 1)  +  Σ_dense α/(60 + rank + 1)
```

with **`hybrid_alpha` α = 0.2** — i.e., 80% weight on lexical rank, 20% on
semantic rank (`retrieval/hybrid_index.py`; configurable 0–1).

**Why:** Code questions overwhelmingly contain exact identifiers ("where
is `GraphBuilder` used"); BM25 nails those. Vector similarity earns its
keep on paraphrases.

**Pros:** Exact-name queries stay precise; paraphrase queries still work.
**Cons:** Pure "concept" questions rank below exact-match noise; raising α
helps those but degrades identifier lookups.

### Reranking — cross-encoder with free fallback

**What:** The top `limit × 5` fused candidates are reranked by a VoyageAI
cross-encoder (`rerank-2.5`). On any provider error — or with no key —
reranking degrades to deterministic local signals, multiplying each score
by:

| Signal | Multiplier |
|---|---|
| Chunk's entity has a docstring | ×1.2 |
| Modified within 7 days | ×1.1 |
| Query appears verbatim in chunk content | ×1.5 |
| Query equals the chunk's entity kind | ×2.0 |

**Pros:** Best ranking quality with a key; graceful, explainable fallback
without one. **Cons:** Fallback favors recently-touched and well-named
code — cold/legacy code ranks lower than it deserves.

### Dependency expansion — graph completes the context

**What:** After ranking, 1-hop callees of the selected entities are pulled
in and labeled `source="dependency"` with score 0.0 — present for
completeness, never as retrieval evidence.

**Pro:** Answers about behavior include what the code calls. **Con:**
Costs tokens; capped at depth 1.

### Routing inside retrieval — exact vs semantic vs lexical

**What:** Queries wrapped in quotes route to an exact-match engine. If
semantic retrieval fails, the orchestrator falls back to lexical search
with keyword extraction, recording the mode used.

## Context synthesis

### Task classification

**What:** A weighted-regex classifier assigns one of six task types —
`explain, debug, extend, review, locate, general` — with
`confidence = min(1, score / (0.5 × max possible))`, boosted up to +30% by
the gap to the runner-up (`llm/query_classifier.py`).

**Pros:** Free, deterministic, no model call. **Cons:** English-pattern
dependent; multilingual or oblique phrasing lands in `general`.

### Task templates and budget packing

**What:** Each task type has a priority order over context sections
(e.g., DEBUG prioritizes source ×2.0 and callers ×1.5; LOCATE wants only
signature/docstring/parent). Sections are packed greedily into the token
budget in priority order; **raw source is capped at 50% of the budget** in
task mode; relationship lists are capped (10 callers/callees, 15 children).
With `summarize=True` (minimal verbosity), raw source is omitted entirely —
the single largest token lever.

### Sufficiency score — the number the product hangs on

**What:** 0.0–1.0 confidence that the bundle is enough to answer locally
(`analysis/context_synthesizer.py`):

```
score       = Σ weight(i) × included(i),  weight(i) = 1/(i+1) over the task's priority list
             + 0.2 if source code included
             + 0.1 if docstring longer than 50 chars
score      *= 0.5 if the whole bundle is under 100 characters
max_score   = Σ weight(i) over the *whole* priority list  +  0.2  +  0.1
sufficiency = min(1.0, score / max_score)
```

`max_score` is fixed by the task type, not by the bundle: both bonuses are in
the denominator whether or not they were earned. That is what makes the number
a measurement — a bundle is scored against what it should have held. Growing
the denominator only alongside the numerator was
[BL-20](../engineering/backlog.md): every bundle holding everything its
template named scored exactly 1.00 whatever the task type, and `extend` and
`locate` cleared the 0.9 gate with no source code at all. A consequence worth
knowing: an entity with no docstring over 50 characters tops out near 0.96, not
1.00, because its bundle really is missing something.

**Why:** It converts "how much context do we have" into a routable number.
**Pros:** Explainable — a low score names the missing sections. **Cons:**
It measures *presence of sections*, not correctness; that gap is exactly
why the routing gate below exists.

### Local-vs-LLM routing — fail-closed by design

**What:** `ask`/`smart_answer` climbs a ladder of retrieval attempts before
touching an LLM: (1) minimal — 1500 tokens, 1 entity; (2) broaden — 3000
tokens, 3 entities, with dependencies; (3) more detail — same breadth at
`verbosity="standard"`. It stops at the first rung whose sufficiency clears
`max(sufficiency_threshold = 0.8, routing_quality_floor = 0.9)`, then answers
locally **only if** the task type is also enabled for local answering;
otherwise it calls the LLM.

The climb is what buys the token saving, and the saving only exists if
stopping early is possible. So the ladder is climbed **only when some task
type may answer locally and the caller has not already demanded the LLM**.
When neither holds, one retrieval runs at rung 3 and the LLM answers from
that — the same single attempt, at full breadth rather than minimal. Gating
the rungs themselves on the local-answer allowlist was
[BL-19](../engineering/backlog.md): it starved the LLM path a control for the
local path was never meant to touch.

Crucially, `local_answer_task_types` is **emptied on every config load**.
It can only be repopulated from a machine-verified policy artifact
(SHA-256-pinned, produced by the separate `knowcode-evals` project whose
gate requires two independent judges, mutation-validated, with a ≥0.90
lower confidence bound). No blessed artifact exists yet.

**Net behavior today: every question escalates to the LLM.**

**Pros:** The "10x token savings" claim can never produce a wrong-but-
confident local answer; the gate is auditable and artifact-based.
**Cons:** The flagship cost-saving flow is inert until the eval program
blesses a policy; users pay LLM costs in the meantime.

### LLM failover and rate limiting

**What:** `ask` iterates configured models in order; a client-side rate
limiter honors per-model RPM/RPD free-tier limits, and provider resource
exhaustion triggers failover to the next model. Prompts follow a strict
contract: instructions travel in the system channel; user question and
retrieved context travel as one JSON-escaped "untrusted input" envelope
with per-field size caps (context 120k chars, question 8k chars) —
repository text is treated as evidence, never instruction
(prompt-injection mitigation).

## Assessment & analysis

### Preflight — 10-dimension report card

**What:** `knowcode preflight` grades the *target repository* (not
KnowCode) on 10 weighted dimensions and maps the weighted mean to a letter:

| Dimension | Weight |
|---|---|
| parse_success_rate | 0.20 |
| documentation_density | 0.20 |
| relationship_density | 0.15 |
| language_coverage | 0.10 |
| naming_quality | 0.10 |
| structural_depth | 0.05 |
| type_annotation_coverage | 0.05 |
| complexity_distribution | 0.05 |
| behavior_analyzability | 0.05 |
| unresolved_references | 0.05 |

Grades: **A ≥ 0.90, B ≥ 0.75, C ≥ 0.55, D ≥ 0.35**, else F. Naming quality
= 40% convention adherence + 30% not-single-char + 30% length factor;
complexity penalizes functions with cyclomatic complexity > 20; type
annotations and behavior analyzability are Python-only (neutral elsewhere).
Weights are configurable and must sum to 1.0 — rejected at config load and
again in `assess_codebase` if they do not, or if any is negative. That was
unenforced until [BL-21](../engineering/backlog.md): the composite divides by
the sum, so a set summing to zero divided by a `1e-9` floor and clamped to a
perfect score, grading an F as an A. Overriding one weight rescales the whole
composite, so a custom set has to rebalance the rest rather than name a single
dimension. `min_score` can turn the report into a hard gate.

**Why:** Sets honest expectations — retrieval is only as good as the
structure in the code. **Pros:** Cheap (reads only the already-built
graph), actionable recommendations. **Cons:** Python-centric in two
dimensions; heuristics reward conventional style, not necessarily good
code.

### Impact risk scoring

**What:** `GET /api/v1/impact/{id}` computes deletion/change risk as the
mean of three 0–1 factors (`storage/knowledge_store.py`):

```
breadth = min(1, (direct_dependents + 0.5 × transitive) / 20)
file    = min(1, affected_files / 5)
type    = 0.3 function · 0.6 class · 0.8 module
risk    = mean(breadth, file, type)
```

**Pro:** Single comparable number across entities. **Con:** Static
structure only — no runtime frequency or ownership data.

## Serving & operations

- **Rate limiting:** 60 req/min standard; 10 req/min for the expensive
  `trace_calls`/`impact` endpoints; buckets keyed on peer IP with proxy
  headers disabled (no `X-Forwarded-For` rotation). The server binds
  `127.0.0.1` by default, so all local clients share one bucket —
  intentionally capping runaway agents.
- **Freshness:** artifact-vs-source comparison flags staleness
  (`is_stale` + reasons) on responses; stale is surfaced, never hidden,
  and never auto-repaired — rebuilding is the user's call.
- **Telemetry:** local, append-only, aggregate-only; the schema makes
  query text and code *unrepresentable* (field allowlists); keyed
  correlation ids (HMAC) allow repeat-question analysis without storing
  questions; 5 MiB × 3 rotations × 30 days; one command deletes
  everything. See [Telemetry & Privacy](../user/telemetry.md).

## Known product limitations

- Local answering disabled pending blessed policy (above).
- Language coverage fixed (12 extensions across 9 language families); others ignored.
- Freshness is advisory. Edits and additions are caught by mtime; deletions
  are caught by comparing the index's file set against the scan, because
  removing a file raises nobody's mtime ([BL-23](../engineering/backlog.md)).
  That comparison runs in one direction only — a scanned file the index does
  not cover is a parse failure, not a deletion — and it is skipped entirely
  for an index whose stored paths cannot be anchored to a repository root,
  since a wrong comparison would invent deletions rather than miss them.
- Unresolved dynamic references (`ref::` targets) degrade tracing and are
  surfaced as a preflight dimension rather than fixed.
- Classifier heuristics are English-oriented.
