# Retrieval Quality Evaluation and Benchmarking

This document describes how to use and extend the KnowCode retrieval evaluation harness and the local benchmark tool.

## 1. Golden Dataset Schema

The golden dataset is stored in [golden_v1.0.json](file:///Users/deepg/Desktop/KnowCode/tests/eval/golden/golden_v1.0.json) as a list of JSON objects:

```json
{
  "query_id": "q1",
  "query_text": "Find the function that parses markdown files.",
  "task_type": "locate",
  "difficulty": "easy",
  "expected_entities": [
    "src/knowcode/parsers/markdown_parser.py::MarkdownParser.parse_file",
    "src/knowcode/parsers/markdown_parser.py::MarkdownParser"
  ],
  "expected_files": ["src/knowcode/parsers/markdown_parser.py"],
  "must_mention_facts": [],
  "must_not_mention_facts": []
}
```

Metadata for the dataset version is stored in [golden_v1.0.meta.json](file:///Users/deepg/Desktop/KnowCode/tests/eval/golden/golden_v1.0.meta.json) to guard against codebase line range drift.

### How to Add a New Query
1. Open [golden_v1.0.json](file:///Users/deepg/Desktop/KnowCode/tests/eval/golden/golden_v1.0.json).
2. Append a new object specifying the `query_id`, `query_text`, `task_type`, `difficulty`, `expected_entities` (as relative project paths `file_path::symbol`), and `expected_files` (as relative file paths).
3. Increment the `query_count` in [golden_v1.0.meta.json](file:///Users/deepg/Desktop/KnowCode/tests/eval/golden/golden_v1.0.meta.json).

---

## 2. Pytest Quality Evaluation Suite

The primary regression gate is wired into the pytest suite. The quality tests assert baseline MRR, Recall@10, Precision@1, and File Coverage.

To run the suite locally or in CI:

```bash
VOYAGE_API_KEY_1="<your-key>" uv run pytest tests/eval/harness/test_retrieval_quality.py -v
```

### Supported Assertions
- **Mean MRR**: Must be `>= 0.40`.
- **Mean Recall@10**: Must be `>= 0.50`.
- **Mean File Coverage@5**: Must be `>= 0.50`.
- **Easy Queries Precision@1**: Every easy query must rank a correct entity at Rank 1.
- **Locate MRR**: LOCATE task-type queries must have mean MRR `>= 0.60`.
- **No Zero Recall**: No task type is allowed to have 0% recall@10.

---

## 3. Local Evaluation Script

The script [scripts/evaluate.py](file:///Users/deepg/Desktop/KnowCode/scripts/evaluate.py) provides a CLI interface to run evaluations, output per-query and aggregate metrics as JSON, and optionally fail if quality drops below a given threshold.

### CLI Usage

```bash
uv run python scripts/evaluate.py <ground_truth.json> <index_dir> [--threshold <mrr_floor>]
```

**Example:**

```bash
VOYAGE_API_KEY_1="<key>" uv run python scripts/evaluate.py tests/eval/golden/golden_v1.0.json knowcode_index --threshold 0.4
```

### JSON Output Schema

The output contains:
- `queries`: Number of evaluated queries.
- `precision_at_1`, `precision_at_5`, `recall_at_10`, `mrr`: Mean aggregate metrics.
- `results`: Detailed breakdown of each query containing the query ID, MRR, precision@1, expected entities, and the top 5 retrieved entities.

---

## Appendix A: Golden Dataset Pipeline Design Spec


**Status:** Design spec — Phase 1 not yet executed
**Owner:** TBD
**Schema version:** `1.0.0`
**Target artifact:** `tests/eval/golden/golden_v1.0.json`

---

## 1. Purpose & Scope

KnowCode's product-critical claim — *"If `sufficiency_score >= 0.8`, answer locally with zero external tokens"* — is an empirical claim about the relationship between retrieved context and answer correctness. That relationship has never been measured end-to-end against ground truth.

This pipeline builds the ground truth.

**What the golden dataset is:** a committed, versioned set of `(query, canonical_answer, expected_entities)` records, each with structural fields that are mechanically verifiable against the codebase.

**What it is used for:**
- Regression testing retrieval quality (precision@k, recall@k, MRR)
- **Calibrating** `sufficiency_score` against actual answer correctness
- Gating CI on measured retrieval + routing quality
- Giving every future change in indexing, chunking, reranking, or synthesis a comparable baseline

**What it is not:**
- A corpus for training retrieval models
- A benchmark for natural-language generation quality
- A substitute for production telemetry

**Scope for Phase 1:** evaluate retrieval + sufficiency routing on **KnowCode's own codebase**. KnowCode is Python, is parseable, and the ground truth is auditable by reading the source. It is the correct starting corpus because the evaluator has full inspection access.

---

## 2. Design Principles

These principles are non-negotiable and every downstream decision follows from them:

1. **The codebase is the oracle of first resort.** Any claim that can be resolved against the AST (entity IDs, file paths, line ranges, caller/callee relationships) is verified structurally before any LLM judgment enters the pipeline. LLM consensus is used only for natural-language narrative equivalence, never for structural correctness.

2. **Asymmetric agent roles, not symmetric peer review.** Peer review between two identical-class LLMs amplifies shared biases and systematically removes the hardest queries from the dataset (because agreement correlates with easiness). The pipeline uses three distinct roles — Author, Oracle, Adversary — each with a different objective function.

3. **Mechanical validation gate precedes LLM judgment.** Every structural claim in an oracle answer must resolve in the parsed codebase. Non-resolving answers fail automatically with no vote.

4. **Stratified difficulty, enforced at the sampling stage.** Without explicit stratification, agent pipelines produce easy-query datasets that fail to differentiate good retrieval from mediocre retrieval. The sampling plan is committed before the pipeline runs.

5. **Human spot-check gate before any version is blessed.** No fully-automated dataset is ever tagged as `golden_v1.0`. A minimum fraction is reviewed by a human, and a committed error-rate ceiling triggers a requeue.

6. **Structured output over prose.** Oracle answers are JSON-first. The narrative is a field, not the substance. Scoring is done on structured fields; narrative is rated separately and only after structure passes.

7. **Small and trustworthy beats large and dubious.** Phase 1 targets 60 labelled queries with ≥ 15% human review. Scale comes only after the bootstrap batch is calibrated.

---

## 3. The Three-Role Pipeline

Each role is an agent with a fixed system prompt, a specific toolset, and a specific output schema.

### 3.1 Query Author

**Objective:** Generate diverse, stratified queries grounded in specific codebase entities.

**Inputs:**
- Codebase scope (directory, optionally filtered)
- Task type (`locate` | `explain` | `debug` | `extend` | `review` | `general`)
- Difficulty tier (`easy` | `medium` | `hard`)
- Optional focus area (module path, feature name)
- Diversity constraints (avoid queries structurally similar to already-emitted ones)

**Tools:** File listing, ripgrep-style search, file reading. **No KnowCode retrieval tools** — the author must pick queries by directly reading the code so the final benchmark is not circular with the thing being benchmarked.

**Output:** `QueryCandidate` records (see §4.1).

**Hard constraints:**
- Every query must name at least one primary target entity (`entity_id`) grounded in the real codebase.
- Queries must be phrased the way a real developer would phrase them — no use of internal qualified names unless natural.
- A query whose answer could be produced by grep alone is only acceptable at difficulty `easy`.

### 3.2 Oracle Answerer

**Objective:** Produce the canonical structured answer for a candidate query **by reading the code directly**, not by memory.

**Inputs:**
- A single `QueryCandidate`
- Full codebase access

**Tools:** File reading, ripgrep, AST inspection (Python `ast` module is acceptable; calling KnowCode itself is forbidden because it creates oracle/benchmark circularity). Git `log` and `blame` are permitted for `REVIEW`-type queries.

**Output:** `OracleAnswer` record (see §4.1). The record is JSON-first; the narrative is one field among many.

**Hard constraints:**
- Every `entity_id`, file path, and line range must be copied from code the agent actually read during this session. No answering from prior knowledge.
- The agent must list the **source evidence** it consulted (file paths + line ranges) so the Adversary can re-check the same cells.
- If the oracle is uncertain, it must emit `confidence: "low"` and a `uncertainty_notes` field. Low-confidence records flow to mandatory human review.

### 3.3 Adversarial Reviewer

**Objective:** Try to falsify the Oracle's structured claims. Not "rate the answer" — *break it*.

**Inputs:**
- The `OracleAnswer`
- Full codebase access

**Tools:** Same as Oracle, but instructed to read the source evidence independently and verify each claim one at a time.

**Output:** `AdversaryReport` record: for each structural claim and each natural-language fact in `must_mention_facts`, a verdict of `supported` | `unsupported` | `partial` with a citation.

**Hard constraints:**
- The Adversary must not accept claims by agreement; each claim must be verified against source it reads in-session.
- Any `unsupported` verdict on a structural claim blocks the label from being committed.
- Any `partial` verdict triggers the query for human review.

**What the Adversary is NOT:** a second Oracle whose answer gets "blended" with the first. Disagreement is signal that something is wrong with at least one of them, not an opportunity for averaging.

---

## 4. Canonical Data Schema

### 4.1 Records

All records are JSON Lines (`.jsonl`) unless otherwise noted, written in insertion order for reproducibility.

#### `QueryCandidate`

```json
{
  "query_id": "q_00042",
  "query_text": "Where do we decide whether to answer a query locally or call an LLM?",
  "task_type": "locate",
  "difficulty": "medium",
  "focus_area": "src/knowcode/llm/",
  "target_entity_ids": [
    "src/knowcode/llm/agent.py::Agent.smart_answer"
  ],
  "authored_by": "agent:query-author:sonnet-4.6",
  "created_at": "2026-04-14T12:00:00Z",
  "schema_version": "1.0.0"
}
```

#### `OracleAnswer`

```json
{
  "query_id": "q_00042",
  "task_type": "locate",
  "difficulty": "medium",
  "expected_entities": [
    {
      "entity_id": "src/knowcode/llm/agent.py::Agent.smart_answer",
      "role": "primary",
      "rationale": "Contains the sufficiency threshold check and the local/LLM branch."
    }
  ],
  "expected_files": ["src/knowcode/llm/agent.py"],
  "expected_line_ranges": [
    {"file": "src/knowcode/llm/agent.py", "start": 160, "end": 221}
  ],
  "must_mention_facts": [
    "The gate compares `avg_sufficiency` against `self.config.sufficiency_threshold`.",
    "When the gate passes, `_format_local_answer` is called and no LLM client is invoked.",
    "When the gate fails (or `force_llm=True`), `self.answer(query)` is called."
  ],
  "must_not_mention_facts": [
    "The decision is made in `ContextSynthesizer` (it is not — synthesizer only *computes* the score).",
    "The threshold is hardcoded to 0.8 (it is not — it is configurable via `AppConfig.sufficiency_threshold`)."
  ],
  "canonical_narrative": "The local-vs-LLM decision lives in Agent.smart_answer (src/knowcode/llm/agent.py:160-221). It retrieves context via `self.service.retrieve_context_for_query(query)`, reads the averaged `sufficiency_score` from the retrieval result, and compares it to `self.config.sufficiency_threshold`. If the score meets the threshold and `force_llm` is false, it dispatches to `_format_local_answer` and returns `source='local'`. Otherwise it calls `self.answer(query)` and returns `source='llm'`.",
  "source_evidence": [
    {"file": "src/knowcode/llm/agent.py", "start": 160, "end": 221, "read_at_step": 1},
    {"file": "src/knowcode/config.py", "start": 0, "end": 80, "read_at_step": 2}
  ],
  "confidence": "high",
  "uncertainty_notes": null,
  "authored_by": "agent:oracle:gpt-5",
  "created_at": "2026-04-14T12:01:30Z",
  "schema_version": "1.0.0"
}
```

#### `AdversaryReport`

```json
{
  "query_id": "q_00042",
  "claim_verdicts": [
    {
      "claim_type": "entity",
      "claim": "src/knowcode/llm/agent.py::Agent.smart_answer",
      "verdict": "supported",
      "citation": {"file": "src/knowcode/llm/agent.py", "start": 160, "end": 164}
    },
    {
      "claim_type": "must_mention_fact",
      "claim": "When the gate passes, _format_local_answer is called and no LLM client is invoked.",
      "verdict": "supported",
      "citation": {"file": "src/knowcode/llm/agent.py", "start": 194, "end": 207}
    }
  ],
  "overall_verdict": "supported",
  "blocking_issues": [],
  "reviewed_by": "agent:adversary:sonnet-4.6",
  "reviewed_at": "2026-04-14T12:03:00Z",
  "schema_version": "1.0.0"
}
```

#### `GoldenLabel` (final committed record)

```json
{
  "query_id": "q_00042",
  "query_text": "Where do we decide whether to answer a query locally or call an LLM?",
  "task_type": "locate",
  "difficulty": "medium",
  "expected_entities": [...],
  "expected_files": [...],
  "expected_line_ranges": [...],
  "must_mention_facts": [...],
  "must_not_mention_facts": [...],
  "canonical_narrative": "...",
  "provenance": {
    "query_author": "agent:query-author:sonnet-4.6",
    "oracle": "agent:oracle:gpt-5",
    "adversary": "agent:adversary:sonnet-4.6",
    "human_reviewed": true,
    "human_reviewer": "deepg",
    "reviewed_at": "2026-04-14T13:00:00Z"
  },
  "schema_version": "1.0.0"
}
```

### 4.2 Directory Layout

```
tests/
  eval/
    pipeline/
      00_candidates.jsonl           # QueryCandidates from Phase A
      01_oracle_answers.jsonl       # OracleAnswers from Phase B
      02_adversary_reports.jsonl    # AdversaryReports from Phase C
      03_validation_failures.jsonl  # Mechanical-gate rejects
      04_human_review_queue.jsonl   # Queries needing human eyes
      05_rejected.jsonl             # Final rejects with reason
    golden/
      golden_v1.0.json              # Blessed dataset (JSON, not JSONL)
      golden_v1.0.meta.json         # Version metadata (counts, date, hashes)
    harness/
      test_retrieval_quality.py     # pytest entry point
      scorer.py                     # Structural + narrative scoring
      calibration.py                # Sufficiency calibration fit
```

The `pipeline/` directory is append-only history; each run produces timestamped subdirectories so batches remain reproducible.

---

## 5. Mechanical Validation Gate

This gate runs after the Oracle step and before the Adversary step. It is pure code — no LLM involvement — and it blocks any record that fails.

**Validation checks:**

1. **Entity ID resolution.** Every `entity_id` must resolve against a fresh `knowcode analyze` of the target codebase. Entity IDs that point at non-existent symbols fail.
2. **File existence.** Every `expected_files[i]` must exist at the committed commit SHA.
3. **Line range validity.** Every `expected_line_ranges[i]` must have `start <= end` and both values within the file's line count.
4. **Source evidence consistency.** Every `source_evidence` entry must exist at the committed SHA and span the lines claimed. No "read a file that doesn't exist" claims.
5. **Entity-to-file consistency.** If an `entity_id` resolves to a symbol, the file component of the ID must appear in `expected_files`.
6. **Narrative coverage.** Every file path and entity name mentioned in `canonical_narrative` must appear in `expected_files` or `expected_entities`. (Prevents the oracle from name-dropping entities that weren't in the structured claims.)

**On any failure:** the record is written to `03_validation_failures.jsonl` with the specific check that failed. The Query Author is NOT re-invoked automatically; a human decides whether to retry the Oracle with a hint or to drop the query.

**Why this runs before the Adversary:** because broken structural claims don't deserve LLM time, and the Adversary's narrative-level critique is meaningless if the structure is already wrong.

---

## 6. Material-Difference Definition

"Material difference" is used at exactly one point in the pipeline: deciding whether the Adversary's report indicates sufficient agreement with the Oracle to commit the label.

**Operational definition:** a label is considered **in agreement** iff both of the following hold:

1. **Structural Jaccard ≥ 0.8.** `jaccard(oracle.expected_entities, adversary_supported_entities) >= 0.8` AND `jaccard(oracle.expected_files, adversary_supported_files) >= 0.8`. This is computed on sets of IDs, not on text.

2. **Narrative equivalence = "equivalent".** A third LLM is prompted with `(query, oracle.canonical_narrative, adversary.findings)` and asked to rate agreement on a 3-point scale: `equivalent` | `minor_divergence` | `significant_divergence`. Only `equivalent` passes.

Both conditions must hold. If either fails, the query goes to `04_human_review_queue.jsonl`.

**Deliberately not used:** embedding cosine similarity, BLEU, ROUGE. These reward surface form and routinely misclassify semantic opposites as similar.

---

## 7. Stratification Plan

Stratification is committed **before** the pipeline runs. The Query Author's sampling plan is an input, not an output, and it is machine-checked at commit time.

### 7.1 Task Type × Difficulty Matrix (Phase 1 target: 60 queries)

| Task Type | Easy | Medium | Hard | Total |
|-----------|:----:|:------:|:----:|:-----:|
| LOCATE    |  6   |   4    |  0   |  10   |
| EXPLAIN   |  2   |   6    |  4   |  12   |
| DEBUG     |  2   |   6    |  4   |  12   |
| EXTEND    |  2   |   6    |  4   |  12   |
| REVIEW    |  1   |   5    |  2   |   8   |
| GENERAL   |  2   |   2    |  2   |   6   |
| **Total** | **15** | **29** | **16** | **60** |

**Rationale for the shape:**
- `LOCATE` skews easy because it's the class where KnowCode's local-first mode has the strongest claim. Easy-dominated is correct here — this is the "answer-without-LLM" sweet spot.
- `EXPLAIN`, `DEBUG`, `EXTEND` skew toward medium/hard because that's where retrieval quality determines routing accuracy. A good benchmark differentiates systems here.
- `GENERAL` queries test classifier robustness — deliberate ambiguity, evenly distributed.

### 7.2 Difficulty Definitions

- **Easy:** Single-entity answer. Ground truth is one symbol and one file. Answerable by keyword match.
- **Medium:** 2–3 entities. Requires one hop across the call graph or one cross-reference between modules. Keyword match alone is insufficient; semantic retrieval matters.
- **Hard:** 3+ entities spanning multiple modules, or a cross-cutting concern. Requires multi-hop reasoning or understanding of architectural patterns.

The Query Author is required to include a `difficulty_rationale` field for medium/hard queries explaining why the query fits its tier.

---

## 8. Human Spot-Check Gate

**Minimum review fraction:** 15% of each batch, stratified across task types and difficulty tiers so every (task, difficulty) cell has at least one human-reviewed record.

**Review protocol:**
1. Reviewer reads the query, the oracle answer, and the cited source code side by side.
2. Reviewer fills in a 4-point verdict per record: `correct` | `minor_issue` | `major_issue` | `wrong`.
3. Reviewer may edit `must_mention_facts`, `must_not_mention_facts`, and `canonical_narrative` directly. Edits are tracked in provenance.

**Ceilings (all must hold):**
- `major_issue` + `wrong` combined rate must be < 5% of the sample.
- `minor_issue` rate must be < 15% of the sample.
- Zero `wrong` verdicts on `easy` queries. (Easy queries that pass the mechanical gate but fail human review indicate a broken pipeline, not a hard query.)

**On ceiling breach:** the entire batch is requeued. Agents are re-prompted with the specific failure modes found in review. A breach is always pipeline feedback, never a signal to lower the threshold.

**When Phase 1 is blessed:** after the spot-check passes and a second reviewer independently confirms the sampled records, `golden/golden_v1.0.json` is written and a migration SHA is recorded in `golden_v1.0.meta.json`.

---

## 9. Seed Prompts

Each seed prompt is a complete system prompt for an agent. The curly-brace fields are substituted at runtime.

### 9.1 Query Author — Per Task Type

All Query Author prompts share this preamble:

> You are a senior software engineer generating a retrieval-benchmark query. You must read actual code before emitting a query — no queries from memory. Your output must be a single JSON object matching the `QueryCandidate` schema. You have file-listing, search, and file-reading tools, but you do NOT have access to KnowCode's retrieval tools. You must not write queries whose answer is already trivially encoded in the file structure (e.g., "where is `class Foo`" when `Foo` is the only symbol in a file named `foo.py`).

#### LOCATE

```
Task type: LOCATE
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: pick an entity (function, class, method) in the scope and write a
natural-language query a developer would ask when trying to find this entity
WITHOUT knowing its exact name.

Constraints by difficulty:
- easy:   target is ONE entity whose name directly suggests its purpose.
          Query must NOT contain the entity's exact identifier.
- medium: target is ONE entity whose purpose requires reading the body to infer.
          Query must describe behavior, not syntax.
- hard:   target is 2-3 entities spread across modules that together implement
          one feature. Query should name the feature, not any entity.

Phrasing variation (rotate across emissions):
- Behavioral: "Where do we <verb> <object>?"
- Feature-based: "Show me the <feature-name> code."
- Bug-context: "I need to change how we <behavior> — where is that?"
- Onboarding: "A new engineer is looking for <capability>, where should they start?"

Anti-patterns (reject your own draft if any hold):
- The query contains the exact entity identifier.
- The query is answerable by a single `grep entity_name`.
- The query is ambiguous enough that 5+ unrelated entities would plausibly match.

Output the QueryCandidate JSON. Include a `difficulty_rationale` field for
medium/hard.
```

#### EXPLAIN

```
Task type: EXPLAIN
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: find a function or workflow whose behavior is non-trivial, then
write a query asking how it works.

Constraints by difficulty:
- easy:   ONE function with a clear stepwise body (<40 LOC). Query asks for
          a walkthrough.
- medium: A function that calls 2-4 other functions to accomplish a coherent
          task. Query asks for a step-by-step explanation.
- hard:   A multi-hop control flow crossing module boundaries (e.g., API ->
          service -> store -> response). Query asks for the full end-to-end
          flow.

Phrasing variation:
- "Explain how <X> works."
- "Walk me through what happens when <trigger>."
- "How does <system> do <operation>?"
- "Step-by-step, what is <function>'s behavior?"

Anti-patterns:
- The answer is a one-liner.
- The function is pure glue (e.g., just forwards to another function) unless
  the glue IS the point.
- The query is effectively a LOCATE query in disguise.

Include `target_entity_ids` with ALL entities that must appear in the oracle's
answer. For hard queries, this list is the chain.
```

#### DEBUG

```
Task type: DEBUG
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: find a failure mode in the codebase (raised exception, error
branch, validation failure, edge case) and phrase a query as a real developer
bug report.

Constraints by difficulty:
- easy:   Function raises a specific exception with a clear message. Query
          contains the message text or close paraphrase.
- medium: Error condition is triggered through a call chain (2-3 hops). Query
          describes the symptom without naming the root cause.
- hard:   Cross-module interaction where the bug in module A surfaces as a
          symptom in module B. Query describes the surface symptom only.

Phrasing (must sound like a real bug report):
- "I'm seeing <error message> when I <action>. What's likely causing it?"
- "<Feature> stopped working after <change>. Where should I look?"
- "Why does <behavior> happen when <precondition>?"

Anti-patterns:
- The query contains the entity name of the root cause (too easy).
- The failure is not actually reachable by user action.
- The error is a style/lint issue, not a runtime bug.

`target_entity_ids` must include BOTH the root-cause entity AND the symptom
site for medium/hard queries. The oracle's answer must trace the path.
```

#### EXTEND

```
Task type: EXTEND
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: identify an existing pattern in the codebase and write a query
asking how to add a new instance of it.

Constraints by difficulty:
- easy:   A pattern with 2+ existing examples in ONE file/module (e.g., CLI
          subcommands, pydantic models). Query: "How do I add a new <X>?"
- medium: A pattern spanning 2-3 files (e.g., a new parser requires changes
          to parsers/, graph_builder.py, and tests). Query asks how to add it.
- hard:   A cross-cutting concern requiring changes across 4+ files and
          multiple layers (e.g., adding a new entity metadata field that flows
          through parsing, storage, API, and tests).

Phrasing:
- "How do I add a new <thing>?"
- "Where is the best place to put <new-capability>?"
- "I want to extend <system> to support <case>. How?"

Anti-patterns:
- The "pattern" only has one example (not actually a pattern).
- The extension is purely additive with no integration points (trivial).
- The query asks "what is" rather than "how to add".

`target_entity_ids` must include ALL files/symbols a correct answer must
touch. The oracle's answer is judged partly on completeness of this list.
```

#### REVIEW

```
Task type: REVIEW
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: pick a unit of code (function, class, recent commit) and write a
query asking for a code review.

Constraints by difficulty:
- easy:   One function with an obvious concern (missing error handling,
          unvalidated input, silent exception swallow). Query: "review X".
- medium: Code with subtle invariants (a cache, a lock, a mutable default
          arg, an async boundary). Query asks to audit the invariants.
- hard:   A cross-module contract (e.g., a protocol that two classes must
          satisfy, or a serialization format consumed by a test). Query asks
          whether the contract holds.

Phrasing:
- "Review <entity> — any risks?"
- "Audit <X> for <class-of-issue>."
- "Is <invariant> actually held by <system>?"

Anti-patterns:
- Style/formatting review (not a retrieval benchmark).
- Review of code that's clearly correct.
- "Is this code good" with no specific question.

`must_mention_facts` in the oracle answer will enumerate the actual findings.
A REVIEW query is worthless if the oracle can't list concrete findings.
```

#### GENERAL

```
Task type: GENERAL
Difficulty: {difficulty}
Scope: {focus_area or whole repo}

Your job: craft a query that is deliberately ambiguous across task types.
This probes the classifier and the system's behavior under misrouting.

Constraints:
- Query should be plausible to a real developer but not cleanly classifiable.
- The classifier is expected to return `general` with low confidence.
- You MUST record in `classifier_expectation` what task type you think the
  classifier will (wrongly) pick.

Examples:
- "Tell me about the parser." (locate? explain? extend?)
- "What's going on with the config loading?" (debug? explain? review?)
- "How does knowcode handle Python files?" (explain? locate?)

Output a QueryCandidate with `task_type: "general"` and include
`classifier_expectation` in the metadata.
```

### 9.2 Oracle Answerer Prompt Template

```
You are the Oracle Answerer for a retrieval benchmark. Your job is to produce
the canonical structured answer to a query by reading the actual codebase.

Non-negotiables:
1. You must read source code in this session for every claim you make. No
   answering from memory or prior knowledge.
2. Every entity_id, file path, and line range you emit must come from code
   you personally read in this session. Cite it in `source_evidence`.
3. You do NOT have access to KnowCode's retrieval tools. You have file
   listing, ripgrep, file reading, and Python AST inspection only.
4. Output is a single JSON object matching the `OracleAnswer` schema.
5. If you cannot produce a high-confidence answer after reasonable effort,
   emit `confidence: "low"` with `uncertainty_notes` explaining what you
   tried and what is unclear. Low-confidence records go to human review.

Procedure:
Step 1: Read the query and the provided `target_entity_ids`.
Step 2: Use `read_file` to open each target entity and read its body.
Step 3: For medium/hard queries, trace callers and callees using ripgrep.
        Read each relevant transitively-connected file.
Step 4: Construct `expected_entities` (including the role of each entity:
        primary, supporting, contextual).
Step 5: List `expected_files` and `expected_line_ranges` exactly.
Step 6: Write `must_mention_facts`: 3-8 atomic, verifiable claims. Each
        claim must be checkable against a specific line range.
Step 7: Write `must_not_mention_facts`: 1-3 common mistakes a developer
        could plausibly make about this code.
Step 8: Write `canonical_narrative`: a prose answer (80-250 words) that
        references only entities in `expected_entities`.
Step 9: Record `source_evidence` — every file read during this session.
Step 10: Self-check: does every entity in the narrative appear in
         `expected_entities`? Does every file in line_ranges appear in
         `expected_files`? If not, fix and re-emit.

Query to answer:
{query_candidate_json}
```

### 9.3 Adversarial Reviewer Prompt Template

```
You are the Adversarial Reviewer for a retrieval benchmark. Your job is to
TRY TO FALSIFY the Oracle's structured claims. You are not a second Oracle
and your output is not "another answer" — you are reviewing the Oracle's
answer against the actual source code.

Non-negotiables:
1. For every claim in the Oracle's answer, you must independently read the
   cited source code and decide whether the claim holds.
2. You must NOT accept a claim because it "looks right." Read the lines.
3. If a claim is partially supported or context-dependent, mark it
   `partial` and explain.
4. You may use file_read, ripgrep, and AST inspection. You do not need to
   redo the Oracle's full investigation — you need to verify its output.
5. Output is a single JSON object matching the `AdversaryReport` schema.

Procedure:
For each expected_entity: verify the symbol exists at the claimed location.
For each expected_file: verify the file exists.
For each expected_line_range: open the file at that range and verify it is
  relevant to the query.
For each must_mention_fact: find the line range that supports it. Record the
  citation. If you cannot find support, mark `unsupported` with what you
  looked at.
For each must_not_mention_fact: verify the negative — that the claimed
  falsehood is actually false.
Finally: does the canonical_narrative reference anything not in
  expected_entities or expected_files? If yes, record as a blocking issue.

Oracle answer to review:
{oracle_answer_json}
```

---

## 10. Execution Flow

### 10.1 Phase 1 — Bootstrap (target: 60 validated queries)

**Step A — Commit the sampling plan.** Author the §7.1 stratification matrix as a file (`tests/eval/pipeline/phase1_plan.json`). The pipeline reads it and emits queries only into the cells it defines. Overflow cells are rejected.

**Step B — Run Query Authors in parallel, one per `(task_type, difficulty)` cell.** Each agent emits 1.5× its cell's target (to allow for rejection headroom). Results append to `00_candidates.jsonl`.

**Step C — De-duplicate candidates.** Reject any candidate whose `target_entity_ids` overlap more than 50% with an already-accepted candidate. This preserves diversity.

**Step D — Run the Oracle Answerer on each accepted candidate.** Results append to `01_oracle_answers.jsonl`. Low-confidence records are flagged for human review but still proceed to the next step.

**Step E — Run the mechanical validation gate.** Pure Python, no LLMs. Rejects go to `03_validation_failures.jsonl` with the specific check that failed.

**Step F — Run the Adversarial Reviewer.** Results append to `02_adversary_reports.jsonl`. Apply the §6 material-difference definition. Passes stay in the candidate pool; any record with blocking issues goes to `04_human_review_queue.jsonl`.

**Step G — Stratified human spot-check.** 15% sample per §8 protocol. Reviewer verdicts append to a `human_reviews.jsonl` adjacent to the batch.

**Step H — Assemble `golden_v1.0.json`.** Combine the surviving records with full provenance. Compute and commit `golden_v1.0.meta.json` containing:
- Total count per (task_type, difficulty) cell
- SHA of each source file at review time
- Agent versions and model identifiers
- Pipeline git SHA
- Human reviewer IDs
- Spot-check error rates

**Step I — Commit to the repo and write the harness.** `tests/eval/harness/test_retrieval_quality.py` loads `golden_v1.0.json`, runs KnowCode against each query, and reports:
- `precision@1`, `precision@5`, `recall@10`, `MRR` on `expected_entities`
- `file_coverage@k` on `expected_files`
- `sufficiency_calibration_curve` — reliability diagram of `sufficiency_score` vs. "local answer contained all `must_mention_facts`"
- `answer_correctness@0.8` — the operational metric for the routing claim

### 10.2 Phase 2 — Scale (after Phase 1 is blessed)

1. **Keep the three-role pipeline** — do not degrade to consensus-only. The reason Phase 1 was expensive was the human review; the pipeline itself is agent-driven.
2. **Use `golden_v1.0` as a drift detector.** Every new batch of 40 queries is scored against the `golden_v1.0` distribution on structural and narrative metrics. Batches whose metric distributions differ significantly from gold are requeued.
3. **Reduce human spot-check to 5%,** stratified, with the ceiling `major_issue + wrong < 3%` on the sample. Tightening the ceiling compensates for reduced coverage.
4. **Retire misbehaving agents.** If a specific Oracle or Adversary model consistently produces rejected records, swap it out and note the swap in `golden_v{n}.meta.json`.
5. **Version the dataset on every meaningful change.** `golden_v1.0` → `v1.1` for additions; `v2.0` for schema changes. The harness always loads the newest version unless pinned.

---

## 11. Metrics & Success Criteria

**Phase 1 is "done" when all of:**
- ≥ 56 of 60 records make it to `golden_v1.0.json` (< 7% attrition).
- Human spot-check passes both ceilings in §8.
- Every (task_type, difficulty) cell has ≥ 80% of its target count filled.
- `tests/eval/harness/test_retrieval_quality.py` runs green on `main` and produces a committed baseline in `tests/eval/golden/baseline_v1.0.json`.
- The calibration curve for `sufficiency_score` is published (even if it reveals the score is poorly calibrated — that's the point).

**Phase 1 reveals a finding — any of:**
- Sufficiency score is well-calibrated → current system is defensible; future work can focus elsewhere.
- Sufficiency score is systematically over-confident → immediate implication: lower the default threshold and/or replace the score with a calibrated composite.
- Sufficiency score is under-confident → the token-savings story is weaker than advertised but the correctness story is safer than feared.
- Retrieval is fine, synthesis is the bottleneck → shifts the priority of Phase 5 work.

All four outcomes are useful. The pipeline's job is to make whichever is true *legible*.

---

## 12. Open Questions / Known Limitations

- **The oracle agents may share biases with the agents KnowCode serves.** Mitigated by (a) not exposing KnowCode's tools to the Oracle, (b) requiring in-session source reads, and (c) the human gate. Not eliminated.
- **KnowCode's own codebase is small** (< 100 files). The benchmark will not probe large-monorepo behavior. Phase 2 should add at least one external codebase (e.g., a medium OSS Python project) once the pipeline is trusted.
- **Line ranges drift as the codebase changes.** `golden_v1.0.meta.json` records the SHA; the harness must fail loudly if it is run against a different SHA without an explicit `--allow-drift` flag.
- **The narrative equivalence judge is itself an LLM.** This is the one unavoidable LLM-as-oracle in the pipeline, and it only runs on structural passes. Its verdicts should be periodically spot-checked during Phase 2.
- **Cost is not modeled.** Phase 1 has roughly 60 × (1 Author + 1 Oracle + 1 Adversary + 0.5 narrative judge) ≈ 210 agent invocations, plus ~9 human-reviewed records. A cost budget should be committed before execution.
- **The pipeline has no opinion on which LLMs play each role.** The three roles should use *different* models where possible to minimise shared-bias failures — e.g., Query Author on Claude Sonnet, Oracle on GPT-5, Adversary on Claude Opus, narrative judge on Gemini. This is a configuration decision, not a design decision.

---

## Appendix B: Benefit Measurement Design Spec


**Status:** Design spec — not yet executed
**Owner:** Solo
**Schema version:** `1.0.0`
**Companion to:** the Golden Dataset Pipeline section of this document, and [`mcp-contract.md`](./mcp-contract.md#appendix-token-overhead-reduction-strategies)

---

## 1. Purpose & Scope

The golden dataset pipeline measures **retrieval quality** — does KnowCode return
the right entities, and is `sufficiency_score` calibrated. That is necessary but
it is *single-arm*: it never compares against the alternative a developer
actually has, which is an agent using native `grep`/`Read`.

This spec measures a different quantity: **realized benefit** — the difference an
agent equipped with KnowCode makes versus the same agent without it, on the same
tasks, net of KnowCode's own token overhead.

**The claim under test:** *Equipping an agent with KnowCode improves task outcomes
and/or lowers total cost, after paying for the per-turn schema tax and the
`context_text` payload.*

**What this is:** a counterfactual (A/B) evaluation design.
**What this is not:** a retrieval-quality benchmark (that is the golden pipeline),
nor a usage tracker (that is P5 telemetry in `OPERATIONALIZATION_PRIORITIES.md`).

---

## 2. Core Definition

Benefit is a **delta on the same task**, not a property of KnowCode in isolation:

```
benefit(task) = outcome(Arm_A, task) − outcome(Arm_B, task)
```

A single blended number across all tasks hides the story and must not be the
headline. Benefit is reported **sliced by the golden dataset's `(task_type,
difficulty)` strata** (§7 of the golden pipeline), because it is expected to be:

- **Large and positive** on multi-hop `EXPLAIN` / `DEBUG` / `EXTEND` (where blind
  grep is expensive and error-prone).
- **Near zero or negative** on `easy` / `LOCATE` (where one `grep` beats a
  ~600-token schema tax plus a `context_text` payload).

A design that cannot show the negative cells is not measuring honestly.

---

## 3. The Two Arms

Both arms use the **same base model, temperature, prompt skeleton, and task set.**
The only manipulated variable is tool availability.

### Arm A — Treatment (KnowCode)

- Agent follows [`mcp-contract.md`](./mcp-contract.md): `retrieve_context_for_query`
  first, verbosity ladder, sufficiency gate.
- Native `grep`/`Read` remain available — the realistic setup is *additive*, not
  KnowCode-exclusive. Forcing KnowCode-only would measure a strawman.

### Arm B — Control (Baseline)

- Identical agent, **KnowCode MCP tools removed from the tool surface.**
- Only native file-listing, ripgrep-style search, and file reading.
- This is the honest counterfactual: what the developer falls back to today.

> The control arm is the artifact that does not exist yet in the repo. The
> `tests/eval/harness/` directory currently holds only the single-arm
> retrieval harness, which auto-skips for lack of a golden dataset.

---

## 4. Metrics

Logged **per task, per arm**, then differenced and aggregated by stratum.

| Dimension | Metric | Definition |
| --- | --- | --- |
| **Correctness** | `success` (0/1) | Scored against the golden `must_mention_facts` / `must_not_mention_facts` for the task. Reuse `tests/eval/harness/scorer.py`. |
| **Cost (net)** | `total_tokens`, `usd` | Prompt + completion + **all tool-call tokens**. KnowCode's schema injection and `context_text` are counted as a **debit**, not excluded. |
| **Effort** | `tool_calls`, `turns` | Round-trips to resolution. Fewer exploratory `grep`/`Read` hops is the *mechanism* benefit flows through. |
| **Latency** | `wall_clock_s` | End-to-end, treatment vs control. |

### 4.1 The net-cost rule (most likely to be gamed by accident)

Do **not** report "tokens saved by answering locally" in isolation. That number
ignores the ~4,500–8,000 tokens/call debit catalogued in
[`mcp-contract.md`](./mcp-contract.md#appendix-token-overhead-reduction-strategies). The only
valid cost metric is:

```
net_token_benefit(task) = total_tokens(Arm_B) − total_tokens(Arm_A)
```

where `total_tokens(Arm_A)` already includes every KnowCode token. A positive
value means KnowCode paid for itself on that task.

### 4.2 Headline metrics

1. **Success-rate lift** per stratum: `mean(success | A) − mean(success | B)`.
2. **Net tokens saved per *successfully completed* task** (conditioning on
   success avoids rewarding a cheap-but-wrong arm).
3. **Effort reduction**: `Δ tool_calls`, `Δ turns`.

Report all three with paired confidence intervals, not point estimates.

---

## 5. Experimental Controls

Benefit is small relative to run-to-run variance, so confounds dominate if
unmanaged:

- **Same model, temperature, system prompt** across arms. The *only* difference
  is the tool surface.
- **≥ N seeds per task per arm** (start N=3). Agentic runs are stochastic; a
  single run measures prompt luck, not KnowCode.
- **Paired analysis.** Compare arms on the *same* task+seed, then aggregate
  deltas. Do not compare arm means computed over different task draws.
- **Fixed codebase SHA**, recorded alongside results, matching the golden
  dataset's SHA guard (golden pipeline §12). Line ranges and entity IDs drift.
- **Blind scoring.** The scorer must not know which arm produced an answer.

---

## 6. Amendments Required to the Golden Pipeline (do this *before* the dataset build)

This is the de-risking payoff of writing the spec first. The golden pipeline
runs ~210 agent invocations to produce 60 labelled queries; it is far cheaper to
add these constraints now than to regenerate the dataset later.

1. **Answerability-by-baseline flag.** Each `GoldenLabel` gains
   `baseline_answerable: bool` — whether a no-KnowCode agent could plausibly
   complete the task at all. Tasks that are *impossible* without KnowCode and
   tasks that are *trivial* with grep both belong in the set, but they must be
   distinguishable, or the benefit average is meaningless.
2. **Cost-accounting fields.** The harness output schema (golden pipeline §10.1
   Step I) must reserve per-arm `total_tokens`, `tool_calls`, `turns`,
   `wall_clock_s`. Add them now so both harnesses share one result format.
3. **Task phrasing audit.** Confirm queries are phrased the way a developer
   asks (already a Query Author constraint, golden §3.1) — a query that leaks an
   `entity_id` lets the control arm `grep` straight to the answer and understates
   benefit.
4. **Stratum parity.** The benefit report slices by the *same* `(task_type,
   difficulty)` matrix. No new stratification scheme; reuse §7.1.

None of these change the three-role pipeline. They only widen the committed
output schema.

---

## 7. Execution Order

The dependency chain is explicit:

```
golden dataset (built)
        │
        ├──► single-arm retrieval harness   (exists, auto-skips today)
        │
        └──► control-arm benefit harness    (this spec; build after data exists)
```

1. Apply §6 amendments to `GOLDEN_DATASET_PIPELINE.md`.
2. Execute golden Phase 1 → `golden_v1.0.json`.
3. Extend `tests/eval/harness/` with a second runner that executes both arms per
   task and emits the §4 metrics. Reuse `scorer.py` unchanged.
4. Publish the per-stratum benefit table as the committed baseline artifact.

Building the control-arm harness *before* step 2 is premature — it would be a
second skeleton with no data to run against.

---

## 8. Reporting Format

A committed `benefit_v1.0.json` plus a human-readable table:

| Task type | Difficulty | n | Δ success | Net tokens/task | Δ tool_calls | Verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| EXPLAIN | hard | … | … | … | … | pays off |
| LOCATE | easy | … | … | … | … | net cost |

The verdict column states plainly where KnowCode earns its keep and where it does
not. "Net cost on easy LOCATE" is a valid, expected, publishable result — it
tells the routing layer where *not* to invoke retrieval.

---

## 9. Non-Goals & Honest Caveats

- **This does not measure production benefit.** It measures benefit on a
  curated 60-query set on KnowCode's own small (<100 file) codebase. Generalizing
  to large monorepos requires the Phase 2 external corpus (golden §12).
- **Scorer fidelity bounds everything.** If `scorer.py` mis-grades, both arms are
  mis-graded; the *delta* is more robust than either absolute, but not immune.
- **Agent-skill confound.** A weak control-arm agent (bad at using grep) inflates
  benefit. The control agent must be a competent grep user, not a strawman.
- **Benefit is model-dependent.** A model with a larger context window or better
  native code search may show smaller benefit. Record the model; do not
  generalize across models without re-running.
