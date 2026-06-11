# Prose Retrieval Golden Set (SDLC document collateral)

**Status: SPEC ONLY — awaiting corpus.** No prose corpus is present in this repo yet, so no records exist and **nothing here runs in CI** (this directory contains data + schema + docs, no `test_*.py` for pytest to collect). This is the measurement scaffold that must exist *before* any retrieval tuning, mirroring the code eval set.

This is the prose sibling of the code eval pipeline (`tests/eval/golden/`, `tests/eval/pipeline/`, `tests/eval/harness/`). Full design and metrics: [`docs/research/document_collateral_retrieval_2026.md` §6](../../../docs/research/document_collateral_retrieval_2026.md).

## Files

| File | Role |
|---|---|
| `phase1_plan.json` | Committed stratification plan (machine-checked input). Mirror of `tests/eval/pipeline/phase1_plan.json`, keyed on `query_type × difficulty` with doc-type coverage constraints. |
| `golden_v0.schema.json` | JSON Schema (draft 2020-12) for a final prose `GoldenLabel`. Extends the code record: `query_type` replaces `task_type`; `expected_doc_spans` replace `expected_entities`/`expected_files`; traceability/synthesis add `expected_trace_path` / `expected_doc_types`. |
| `examples.jsonl` | Three placeholder records (one per query type) that validate against the schema. `method: "template_placeholder"`, `source_verified: false` — illustrative only, **not** labels. |

## Query types & stratification (target: 60)

| Query type | easy | medium | hard | total |
|---|:--:|:--:|:--:|:--:|
| `fact_lookup` | 8 | 8 | 4 | 20 |
| `traceability` | 0 | 8 | 12 | 20 |
| `synthesis` | 2 | 8 | 10 | 20 |

- **fact_lookup** — answer in one passage of one doc; the local-first / precise-retrieval sweet spot.
- **traceability** — multi-hop across the SDLC chain (Problem Statement → PRD → HLD → LLD → Release Notes → Support); the class flat RAG cannot answer and the document graph owns. No `easy` tier by definition.
- **synthesis** — aggregate/compare across many docs (e.g. "summarize all healthcare case studies", "what changed v2→v3").

## Cross-field gates (enforced by the harness, not the JSON Schema)

- `line_range` is `[start, end]` with `start <= end` and both within the file at the recorded SHA.
- Every `doc_id` + `heading_path` resolves against a fresh parse of the corpus.
- traceability: `expected_trace_path` spans ≥ 3 distinct doc types; every node resolves in the document graph.
- synthesis: `expected_doc_spans` cover ≥ 3 distinct documents.

## Scoring (when the corpus exists)

Reuse `tests/eval/harness/scorer.py` with prose extensions:
- **Retrieval:** `recall@k` (primary — a missed span = a wrong generated answer), `nDCG@10`, `MRR`.
- **Traceability:** path-completeness (fraction of `expected_trace_path` recovered).
- **Synthesis:** document-coverage over `expected_doc_spans`.
- **Generation:** faithfulness/groundedness + citation accuracy over `must_mention_facts` / `must_not_mention_facts`.
- **Routing:** re-calibrate `sufficiency_score` against prose answer-correctness — do **not** port the code corpus's 0.8 threshold; re-measure (the code finding was over-confident).

## Bootstrap procedure (blocked on corpus)

1. Point the corpus at a sample doc set (the only missing input).
2. Run the three-role Author / Oracle / Adversary pipeline; the Oracle reads documents instead of code.
3. Mechanical gate resolves `doc_id` + `heading_path` + `line_range` and `feature_id`/`requirement_id` joins.
4. Stratified human spot-check (15%), then bless `prose_golden_v1.0.json` with a `meta.json` recording per-doc SHAs (same drift guard as the code set).
