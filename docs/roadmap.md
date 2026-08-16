# KnowCode Roadmap

> **Status:** Active planning document
>
> The completed v1.1 operationalization work is preserved in the repository
> at `docs/archive/MCP_operationalization.md`. This document defines the
> next priorities rather than reopening completed foundation work.

## Current Position

KnowCode already has the core operational foundations: a canonical MCP
contract, freshness reporting, supported-language checks, `knowcode doctor`,
local telemetry, and an MCP handshake check. The next release should turn
those foundations into a calibrated, verifiable, and easy-to-adopt product.

The most important open evidence is retrieval routing quality. The committed
60-record corpus is now explicitly calibration-only, with atomic facts,
prohibited claims, and AST-resolved source citations. It is not a locked
holdout and cannot enable local answering. Runtime routing therefore defaults
to an empty task allowlist until independent machine adjudication and the
blocking Python external gates pass. See [Testing & Evaluation](engineering/testing.md)
for the evidence contract.

## Release Principles

1. Correctness is the release gate. Token savings, consumer installers, and
   lifecycle convenience must not make stale or uncalibrated answers appear
   trustworthy.
2. A green `knowcode doctor --mcp` is necessary but not sufficient. It proves
   local artifacts and the MCP transport are healthy; retrieval evaluation and
   freshness/coverage tests prove that the answer path is trustworthy.
3. The canonical MCP contract remains the single policy source. Runtime code,
   agent rules, setup documentation, and tests must agree on it.
4. Preserve compatibility deliberately. Changes to MCP tool names or response
   shapes require a documented migration path and regression coverage.
5. Keep telemetry local by default, make its retention and privacy tradeoffs
   explicit, and use measured data before changing thresholds or budgets.

## Completed Foundations

| Foundation | Current evidence | Ongoing regression gate |
| --- | --- | --- |
| MCP operating contract | [MCP Contract](mcp-contract.md) documents minimal-first retrieval and the escalation ladder. | Contract tests must exercise production-like minimal responses. |
| Freshness and coverage safety | The scanner, watcher, and `doctor` validate source coverage and stale artifacts. | Modify/create/delete/rename tests and `doctor` freshness checks remain required. |
| Local readiness verification | `knowcode doctor --mcp` checks config, artifacts, disk use, agent rules, and an MCP handshake. | Doctor must stay fast, deterministic, and actionable. |
| Local telemetry | JSONL events record retrieval, agent-routing, and MCP tool activity. | Event-schema compatibility and failure isolation tests remain required. |

## Priority Workstreams

### P1 - Hybrid Machine-Verified Trust Gate

**Goal:** turn the existing evaluation harness into a release-quality source of
truth for local-answer routing.

**Why first:** the current evaluation data says a score of `0.8` is
over-confident. No payload, onboarding, or automation improvement can make an
uncalibrated local-answer gate safe.

**Work:**

1. Run the real `Agent.smart_answer` escalation path over source-cited records.
   Require two independently configured judge providers to return strict claim
   verdicts with valid citations, three times each; disagreement, partial
   support, malformed output, missing evidence, or provider failure is a fail.
2. Validate the judges with mechanically generated true/false source mutations.
   Require macro F1 of at least `0.95` and zero false acceptance of critical
   negative canaries.
3. Select a global threshold from `0.50` through `1.00`. Enable a task type only
   when a locked holdout has at least 29 routed cases, zero critical failures,
   and a one-sided exact-binomial 95% correctness lower bound of at least `0.90`.
4. Make Python RepoBench-R archive/v0 and RepoQA blocking. Compare KnowCode with
   BM25 over the identical candidate corpus and token budget; every primary
   metric's seeded paired-bootstrap lower bound must be at least `-0.02`.
5. Track fixed CrossCodeEval-100 and SWE-bench-Lite-50 A/B suites as non-blocking
   downstream evidence until three consecutive runs support an explicit
   promotion decision.
6. Publish a versioned machine-verification artifact with the selected policy,
   floors, source hashes, dataset revisions, provider/model identities, prompt
   hashes, and canary results. Never describe it as human-reviewed.

**Exit criteria:** the routing policy is source-verified, independently
machine-adjudicated, externally benchmarked, versioned, and enforced by CI.
Missing credentials, source or dataset drift, judge instability, BM25
inferiority, and blessed-baseline regression all fail closed.

**Progress (2026-07-31):** the proof and validation harness has moved to the
independent `knowcode-evals` repository. KnowCode now contains only a
checksum-pinned, source-bound schema 1.1 policy consumer and runtime
enforcement tests. The evaluator owns the datasets, judges, benchmark adapters,
statistics, workflow, and artifact issuance. P1 remains deliberately unblessed
because the 60 records are calibration data and no qualifying locked holdout
plus full external baseline has yet passed.

### P2 - Enforce Runtime Contract Conformance

**Goal:** prove the canonical MCP policy is what production code actually does.

**Why now:** the documentation and runtime had drifted: the direct LLM agent
requested the default minimal projection while still reading metadata that
minimal responses omit. Contract tests must keep covering the real projection,
not only mocks that return diagnostic-style payloads.

**Work:**

1. Make `Agent.answer` and `Agent.smart_answer` explicitly request the metadata
   they need, or derive task metadata without relying on omitted fields.
2. Add end-to-end tests for minimal retrieval, budget/verbosity escalation, and
   local-versus-LLM routing using the actual orchestrator response shape.
3. Make `knowcode doctor --mcp` and the release checklist reference the same
   tool, defaults, threshold ownership, and response guarantees as the
   canonical contract.

**Exit criteria:** agent, MCP, CLI, docs, and tests use one contract with no
implicit reliance on fields hidden by minimal mode.

**Progress (2026-08-11):**

- `Agent.answer` and `Agent.smart_answer` now request the minimal projection
  with the task metadata they consume.
- `smart_answer` follows the contract escalation ladder: narrow minimal,
  broader minimal, then standard detail before LLM fallback, while reusing the
  final retrieval instead of querying a fourth time.
- Integration coverage now exercises local and LLM routing against the actual
  `RetrievalOrchestrator` projection.
- **Done:** `knowcode doctor --mcp` and the `docs/engineering/release.md` conformance audit are completed, validating the canonical tool `retrieve_context_for_query` and minimal projection response formatting.

### P3 - Finish the MCP Token Diet

**Goal:** reduce recurring MCP schema and context costs without weakening the
calibrated correctness floor.

**Dependencies:** P1 and P2. Evaluate every change against the blessed golden
baseline and routing-quality gate.

**Work:**

1. Prototype one `knowcode` MCP tool with an `action` enum for `search`,
   `context`, `trace`, `query`, and `quality`. Keep the existing five tools
   available behind an explicit compatibility option for one release.
2. Define response profiles that are summary-first for exploratory work and
   expose raw source only for explicit source requests or task types that need
   it, such as debugging and review.
3. Add byte/token caps per action to regression tests and record payload-size
   distributions in local telemetry.

**Exit criteria:** default tool/result payloads are measurably smaller, golden
retrieval and routing quality do not regress, and migration guidance is
published before the legacy tool surface changes.

### P4 - Unified Agent Onboarding

**Goal:** make the canonical MCP policy and connection setup installable rather
than a collection of hand-maintained per-agent files.

**Dependencies:** P2; P3 if the consolidated tool becomes the default surface.

**Work:**

1. Add a product-owned `.knowcode/agent-rules.md` that references the canonical
   contract and distinguishes semantic queries from direct file/grep work.
2. Add an idempotent `knowcode install-agent <consumer>` flow for verified MCP
   consumers, beginning with the clients actively supported by the project.
3. Add `knowcode doctor --agent <consumer>` checks for generated configuration,
   rule inclusion, and an end-to-end tool invocation where the client supports
   programmatic verification.
4. Treat each client configuration as an explicit compatibility target. Do not
   claim support for a consumer until its current configuration syntax and
   runtime behavior are verified.

**Exit criteria:** a supported consumer can be configured predictably from one
command, its rules point to the canonical policy, and doctor can identify a
broken setup with an actionable fix.

### P5 - Lifecycle Automation for Solo Repositories

**Goal:** reduce the chance that ordinary repository changes leave KnowCode
artifacts stale while preserving explicit user control.

**Dependencies:** P2. The existing freshness safety remains the fallback if
automation is unavailable or fails.

**Work:**

1. Add an optional repo-local freshness manifest that records the source state
   used to build the store and index, including Git state where available.
2. Offer opt-in post-commit and background refresh helpers. They must never
   block a commit, silently modify unrelated configuration, or hide a refresh
   failure.
3. Document optional always-on watch-service templates for supported local
   environments, with a normal foreground fallback.

**Exit criteria:** users can opt into low-friction refresh automation, while
every failure path remains visible through freshness metadata and `doctor`.

### P6 - Make Usage Observability Usable

**Goal:** turn existing JSONL telemetry into a local decision tool.

**Dependencies:** P1 and P2, so summary metrics use calibrated routing terms.

**Work:**

1. Add `knowcode stats --usage [--since <duration>]` backed by the existing
   telemetry summary support.
2. Report calls per day, routing rate, mean sufficiency, stale-response count,
   payload-size distribution, and available per-consumer attribution.
3. Add clear retention, redaction, and deletion guidance for local query logs.

**Exit criteria:** a developer can understand whether KnowCode is used, trusted,
fresh, and cost-effective without manually parsing JSONL.

## Sequencing and Gates

`P1` and the implementation work in `P2` can proceed in parallel, but the
release-candidate evaluation must run against the `P2` production response
shape. `P3` follows once the correctness baseline and contract are stable.
`P4`, `P5`, and `P6` can proceed independently after their listed dependencies
are met.

The next trust release ships only when all of the following are true:

1. P1 and P2 exit criteria are met.
2. The full test suite, including the server-extra API contract suite, passes.
3. `knowcode doctor --mcp` passes for the supported local configuration.
4. Freshness and language-coverage checks report no unresolved correctness
   warnings for the target repository.

P3 through P6 improve efficiency and adoption, but they are not permitted to
weaken these release gates.

## Out of Scope

- A shared or hosted multi-tenant knowledge service.
- Making the HTTP gateway the primary path for this roadmap.
- Automatically rewriting agent configuration without an explicit user command.
- Declaring compatibility for an agent consumer before its current setup and
  invocation behavior are verified.
- Cost optimization that bypasses the retrieval and routing-quality gates.
