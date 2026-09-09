# KnowCode Roadmap

> **Status:** Active planning document. Open work only.
>
> Why the project is where it is — dated decisions, shipped-work narrative, and
> the measured reasons behind them — is
> [Roadmap History](engineering/roadmap-history.md). When an item here ships,
> strike it with a one-line dated note and move the narrative there.

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

Defects that no workstream owns are recorded in the
[engineering backlog](engineering/backlog.md). Put the next finding there.

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

## Standing Regression Gates

These hold for every release. The evidence that established them is in
[Roadmap History](engineering/roadmap-history.md).

- **MCP operating contract.** Contract tests must exercise production-like
  minimal responses.
- **Freshness and coverage safety.** Modify/create/delete/rename tests and
  `doctor` freshness checks remain required.
- **Local readiness verification.** `doctor` must stay fast, deterministic, and
  actionable.
- **Local telemetry.** Event-schema compatibility and failure isolation tests
  remain required.
- **Derived vector plane.** A published generation must contain nothing named
  `vectors.*`, and a rebuilt plane must return identical results to a persisted
  one.

## Priority Workstreams

### P1 - Hybrid Machine-Verified Trust Gate

**Goal:** turn the existing evaluation harness into a release-quality source of
truth for local-answer routing.

**Why first:** the current evaluation data says a score of `0.8` is
over-confident. No payload, onboarding, or automation improvement can make an
uncalibrated local-answer gate safe.

**Status:** deliberately unblessed. The harness lives in the independent
`knowcode-evals` repository; KnowCode holds only a checksum-pinned policy
consumer and runtime enforcement tests. No qualifying locked holdout plus full
external baseline has yet passed.

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

**Before the next run.** The 60 existing records are void: they describe a
bundle the product no longer builds. Re-collect rather than reusing them
([why](engineering/roadmap-history.md)).

**Threshold input that changed.** The `routing_quality_floor` of 0.90 was
selected against a formula that could only return 1.00 or block. With a fixed
denominator, complete bundles land at 0.95–0.96 and source-less ones at
0.45–0.86. Re-select over that range rather than carrying 0.90 across; the sweep
in work item 3 is where that happens.

**Exit criteria:** the routing policy is source-verified, independently
machine-adjudicated, externally benchmarked, versioned, and enforced by CI.
Missing credentials, source or dataset drift, judge instability, BM25
inferiority, and blessed-baseline regression all fail closed.

### P2 - Enforce Runtime Contract Conformance

**Goal:** prove the canonical MCP policy is what production code actually does.

**Work:** all three original items shipped 2026-08-11 (agent metadata requests,
end-to-end escalation coverage, and a `doctor`/release-checklist conformance
audit). See [Roadmap History](engineering/roadmap-history.md).

**Open residue.** The 2026-08-11 audit validated `retrieve_context_for_query` as
the canonical tool. P3's consolidation then replaced that surface, and the
contract's appendix went on describing the old one until 2026-09-09. The
documents now agree and the contract suites pass against the three-tool surface.
Re-run the [release checklist](engineering/release.md) conformance audit
end-to-end, including a green `doctor --mcp` handshake, before closing P2.

**Exit criteria:** agent, MCP, CLI, docs, and tests use one contract with no
implicit reliance on fields hidden by minimal mode.

### P3 - Finish the MCP Token Diet

**Goal:** reduce recurring MCP schema and context costs without weakening the
calibrated correctness floor.

**Dependencies:** P1 and P2. Evaluate every change against the blessed golden
baseline and routing-quality gate.

**Work:**

1. **End the legacy five-tool surface.** Consolidation shipped as three
   concern-split tools rather than the single `knowcode` tool this item first
   proposed, because client permissions are per-tool. The five flat tools remain
   behind `mcp-server --legacy-tools`, off by default, "for one release" — a
   release this roadmap has never named. Name it, publish the migration
   guidance the exit criteria require, then remove the surface.
2. ~~Summary-first response profiles.~~ Shipped 2026-09-09. The rule now lives
   in the [MCP contract](mcp-contract.md#source-hungry-task-types).
3. ~~Byte/token caps per action, and payload-size distributions in telemetry.~~
   Shipped 2026-09-09.

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

### P7 - Index Footprint and Prose Coverage

**Goal:** make an index proportional to the source it describes, and make every
document in that source retrievable. The plan of record is
[Storage Footprint & Optimization Plan](research/storage_optimization_2026_v4.md);
its §17 is the running execution log and carries the measured ledger.

**Dependencies:** none for the storage phases. The embedding-selection phase
depends on P1, because it narrows the semantic candidate set and may only ship
behind a measured recall gate.

**Governing decision:** the plan's
[DR-4](research/storage_optimization_2026_v4.md) — no phase may cost retrieval
quality, whatever the byte saving.

**Phase F and the int8 ANN cache are rejected, not deferred.** Both are closed
in the [backlog](engineering/backlog.md) with measured reasons. Work item 5's
premise — that an unembedded chunk stays reachable through the exact, path, and
FTS planes — holds only because F is rejected and the exact plane stays.

**Where this leaves the stream.** Everything DR-4 permits inside a generation
has shipped, and one generation now measures 50.78 MB. The larger remaining
lever is retention: two generations hold mostly identical bytes, which is
Phase G, and it is a different axis from making a generation smaller.

**Work, in order:**

1. ~~Document identity and chunking correctness.~~ Shipped 2026-08-29, Phase B.
   Prose coverage 41.6% to 95.7%; the generation fell 2.18 MB while indexing 2.3
   times as much prose.
2. ~~`VACUUM` before the manifest is digested.~~ Shipped 2026-08-29, Phase A2,
   3.79 MB.
3. ~~Lossless encoding.~~ Shipped 2026-08-29, Phase C, 10.0 MB across both
   artifacts.
4. ~~Stop persisting the rest of the derived data.~~ Shipped 2026-08-30,
   Phase D: D2 the contentless FTS5 term index, 4.46 MB; D3
   `entities.source_code` resolved from disk, 3.67 MB.
5. **Embedding-selection policy,** gated on the retrieval evaluation harness.
   Chunks below a content-size threshold stay stored and stay reachable through
   the exact, path, and FTS planes; they leave only the semantic candidate set.
   Under DR-4 it ships on a measured no-loss result, never on a loss judged
   small enough to accept.
6. ~~The lossless remainder.~~ Shipped 2026-09-08, 2.80 MB measured.
7. **Decide Phase G, or retire the plan without it.** Content addressed across
   retained generations, so retention costs the delta rather than the whole.
   Lossless by construction and worth more than everything above combined, but
   larger than the rest of the plan put together. The correctness debt that was
   meant to close before the stream does either way is now paid: a chunk is
   content-addressed by SHA-256 (BL-11) and a staged rewrite witnesses its own
   losslessness (BL-8).

**Exit criteria:** every tracked document in a repository is retrievable, one
generation is a small multiple of the source it describes rather than an order
of magnitude, and no phase past the first changes what an existing query
returns without a measured recall number to justify it. DR-4 tightened the
last clause: a measured recall number is no longer a licence to ship a loss,
only evidence that there is none.

## Sequencing and Gates

`P1` and the implementation work in `P2` can proceed in parallel, but the
release-candidate evaluation must run against the `P2` production response
shape. `P3` follows once the correctness baseline and contract are stable.
`P4`, `P5`, and `P6` can proceed independently after their listed dependencies
are met. `P7`'s remaining storage phases are independent of P1 through P6, and
its embedding-selection phase is gated on P1's evaluation harness.

The next trust release ships only when all of the following are true:

1. P1 and P2 exit criteria are met.
2. The full test suite, including the server-extra API contract suite, passes.
3. `knowcode doctor --mcp` passes for the supported local configuration.
4. Freshness and language-coverage checks report no unresolved correctness
   warnings for the target repository.
5. No `Critical` item is open in the [engineering backlog](engineering/backlog.md).
   Read the backlog rather than trusting any roster written here: a previous
   version of this line named `BL-1` long after Phase B fixed it, and claimed
   nothing was open right up until an audit found more.
6. Preflight on this repository's own graph keeps the unresolved-reference
   resolution rate at 0.790 or better — the [BL-34](engineering/backlog.md)
   baseline, ratcheted. Of the 4,077 holes at that baseline, 2,867 are
   receiver-qualified calls whose file states no type (unannotated
   parameters, cross-function dataflow — a type checker's residue, not a
   linking defect), 1,210 are ambiguous or unknown bare names, and 37 are
   typed-but-unbound holes the evidence reports as
   `unresolved_typed_receivers`; a change to the rate in either direction
   updates the baseline here in the same commit that moved it.

P3 through P7 improve efficiency, adoption, and footprint, but they are not
permitted to weaken these release gates.

## Out of Scope

- A shared or hosted multi-tenant knowledge service.
- Making the HTTP gateway the primary path for this roadmap.
- Automatically rewriting agent configuration without an explicit user command.
- Declaring compatibility for an agent consumer before its current setup and
  invocation behavior are verified.
- Cost optimization that bypasses the retrieval and routing-quality gates.
