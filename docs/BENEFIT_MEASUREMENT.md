# Measuring the Realized Benefit of the KnowCode MCP Server

**Status:** Design spec — not yet executed
**Owner:** Solo
**Schema version:** `1.0.0`
**Companion to:** [`GOLDEN_DATASET_PIPELINE.md`](./GOLDEN_DATASET_PIPELINE.md), [`MCP_TOKEN_OVERHEAD_REDUCTION.md`](./MCP_TOKEN_OVERHEAD_REDUCTION.md)

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
[`MCP_TOKEN_OVERHEAD_REDUCTION.md`](./MCP_TOKEN_OVERHEAD_REDUCTION.md). The only
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
