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

Two things found while measuring index storage change what "next" should mean.
Markdown documents whose H1 slugifies to their own filename are dropped from the
index entirely by an entity id collision, which is 32% of this repository's
prose including seven of eight ADRs and most of the user guide. Separately, the
chunk carrying the largest block of most files is tagged with an entity id no
parser emits, so it is disconnected from the graph. And a published generation
was storing every embedding twice, which P7 has now fixed. Defects
that no workstream owns are recorded in the
[engineering backlog](engineering/backlog.md).

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
| Derived vector plane | [ADR 9](engineering/adr/adr-0009-derived-vector-plane.md): the ANN index is rebuilt from durable chunk rows, not published. One generation fell 113.18 MB to 78.87 MB. | A published generation must contain nothing named `vectors.*`, and a rebuilt plane must return identical results to a persisted one. |

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

- **Done (consolidation):** the default surface is three concern-split tools
  with `action` enums — `knowcode_retrieve`, `knowcode_lifecycle`,
  `knowcode_inspect` — and the five flat tools remain behind
  `mcp-server --legacy-tools` for one release. The split is by concern rather
  than a single tool because client permissions are per-tool, so retrieval can
  be allowlisted while builds stay confirmed.
- **Scope change:** consolidation shipped together with a capability
  expansion (build/index/export/doctor/freshness/stats/history/preflight/
  telemetry/job polling), so the recurring schema cost went from ~650 tokens
  for 5 capabilities to ~1,110 for 14 rather than down to ~200. Cost per
  capability improved ~2.3x; absolute per-turn cost did not fall. A ceiling
  test now guards it. Item 2 (summary-first response profiles) and item 3
  (per-action payload caps in telemetry) remain open, and are where an
  absolute reduction should now come from.
- **Unblocked by the same change:** `mcp-server` no longer refuses to start
  without a store, so an agent can bootstrap a repository through
  `knowcode_lifecycle action='build'` without a terminal step.

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
its §17 is the running execution log.

**Dependencies:** none for the storage phases. The embedding-selection phase
depends on P1, because it narrows the semantic candidate set and may only ship
behind a measured recall gate.

**Governing decision:** the plan's
[DR-4](research/storage_optimization_2026_v4.md) — no phase may cost retrieval
quality, whatever the byte saving. It was settled 2026-08-30 after three
separate phases proposed buying footprint with recall, and it is why two of
them are now rejected rather than deferred.

**Status:** Phases D1, A2, B and all of C shipped 2026-08-29. D1 stopped publishing the
ANN index and made it a plane rebuilt from the durable embeddings in
`chunks.db`, worth 32.31 MB, with retrieval verified unchanged on 6,874 vectors
as identical results, identical top-25 vector ids, and a maximum score delta of
zero. A2 vacuums both databases on the staged copy before the generation is
digested, worth a further 3.79 MB measured over one corpus. Nearly all of A2 is
B-tree repacking rather than free pages, which is why the plan's free-page
estimate read 87% low. B fixed the retrieval defects: prose coverage went from
41.6% to 95.7%, all 55 markdown files now index where 17 were absent, no chunk
points at an entity that does not exist where 495,985 bytes did, and the
generation fell 2.18 MB despite indexing 2.3 times as much prose. C
re-encoded the artifacts without changing what they hold, worth 10.0 MB, and
its last item made a generation's size independent of the directory it was
built in. D2 shipped 2026-08-30: the term index became a contentless FTS5
table carrying `contentless_delete=1`, worth a measured 4.46 MB of `chunks.db`,
with BL-13's deletion fix shipped inside it and the simulator BL-12 flagged
repaired first to re-size the phase honestly. D3 shipped 2026-08-30 and
completed Phase D: `entities.source_code` resolves from the working tree
against its indexed digest, worth a measured 3.67 MB, behind a global
`config.entity_source: disk | stored` (default `disk`) so a repository can opt
back into the persisted copy with no migration. Wiring its resolver into
`get_entity_details` and not into `get_context` cost the canonical MCP tool its
source body on a fresh index; that was BL-16, Critical, found and fixed the
same day.

**Phase F and the int8 ANN cache are rejected, not deferred.** F would have
left `ExactQueryEngine` with no implementation — the term index that would
inherit the mode answers mid-identifier fragments at 14.6% recall at the limit
the engine passes — for 3.50 MB. The int8 cache measured `recall@10 0.9950`
against exhaustive fp32's `1.0000` for 18 MB. Both are closed as rejected in
the [backlog](engineering/backlog.md), and DR-4 records the rule that closed
them.

**Where this leaves the stream.** One generation now measures 46.70 MB.
Everything DR-4 permits inside a generation is worth **2.53 MB**: deflating
`chunks.content` (2.38 MB, the replacement for F) and compacting
`metadata_json` separators (0.15 MB). The larger remaining lever is retention —
two generations hold 97 MB of mostly identical bytes — which is Phase G, and it
is a different axis from making a generation smaller. The plan's §17 closing
ledger carries the full accounting, measured rather than projected; read it
before §11, whose targets are sized against the pre-B corpus.

**Work, in order:**

1. ~~**Document identity and chunking correctness.**~~ Shipped 2026-08-29 as
   Phase B. Three defects that shared one identity scheme, fixed together. A
   section's qualified name now carries its heading path, so a heading cannot
   collide with its own document and 17 rejected markdown files index (BL-1).
   Every chunk hangs on an entity that exists, prose on the section its lines
   fall in and module chunks on the file's own entity (BL-6). `.md` and `.rst`
   route to `ProseChunker`, which had been in the tree, tested, and wired to
   nothing. A class chunk stops at its first member, which is 8.00 MB on its
   own. Two structural contracts now hold the shape rather than the instance:
   unique entity ids per file for all nine parsers, and no chunk pointing at a
   missing entity for seven languages. Writing the first found
   [BL-9](engineering/backlog.md); the Vue gap it left is
   [BL-10](engineering/backlog.md).
2. ~~**`VACUUM` before the manifest is digested.**~~ Shipped 2026-08-29 as
   Phase A2, 3.79 MB. Sizing it revealed that the manifest cannot witness row
   loss in either database, filed as [BL-8](engineering/backlog.md).
3. ~~**Lossless encoding.**~~ Shipped 2026-08-29 as Phase C, 10.0 MB across
   both artifacts. Edges hold three integers against two codebooks (C2, 6.0 MB),
   an entity digest is 32 raw bytes in a column rather than hex inside JSON
   (C3, C4), a chunk digest is a first-class column (C5), and ids are stored
   relative to a recorded repository root (C1, 3.10 MB). C1 is the reason a
   generation no longer costs more when it is built at a deeper path: two builds
   of one corpus differed by 10.9 MB, 19%, and now differ by 0.16 MB. It does
   not make a generation portable. Ids stay absolute above the storage layer and
   resolve against the root recorded in the database, so moving a repository
   still requires a rebuild, as ADR 1 has always said. Sizing C1 found that the
   storage simulator no longer models a post-C2 `knowledge.db`
   ([BL-12](engineering/backlog.md)), and it removed the carrier BL-9 had been
   deferred onto.
4. ~~**Stop persisting the rest of the derived data.**~~ Shipped 2026-08-30,
   completing Phase D. D2: `tokens_text` folds into a contentless FTS5 table
   declared with `contentless_delete=1`, so deletion works and the sync
   triggers are replaced by explicit FTS writes in each chunk transaction
   (BL-13, closed), worth a measured 4.46 MB. D3: `entities.source_code`
   resolves from disk against a verified content hash, failing closed rather
   than serving stale source, worth a measured 3.67 MB — and it ships as a
   global configuration setting, `config.entity_source: disk | stored`
   (default `disk`), so a repository can opt back into the persisted copy per
   build with no migration.
5. **Embedding-selection policy,** gated on the retrieval evaluation harness.
   Chunks below a content-size threshold stay stored and stay reachable through
   the exact, path, and FTS planes; they leave only the semantic candidate set.
   Under DR-4 it ships on a measured no-loss result, never on a loss judged
   small enough to accept. Its premise — that an unembedded chunk stays
   reachable through the other three planes — holds only because F is rejected
   and the exact plane stays.
6. **The lossless remainder, 2.53 MB.** Deflate `chunks.content` (2.38 MB;
   `zstandard` with a trained dictionary reaches 3.05 MB and adds a dependency)
   and dump `metadata_json` with compact separators (0.15 MB across three call
   sites). Neither changes a plane, a semantic, or a freshness contract.
   Independent of P1.
7. **Decide Phase G, or retire the plan without it.** Content addressed across
   retained generations, so retention costs the delta rather than the whole.
   Lossless by construction and worth more than everything above combined, but
   larger than the rest of the plan put together. `BL-11` — chunks
   content-addressed by MD5, where a collision hands one chunk another chunk's
   vector — should close before the stream does either way.

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
are met. `P7`'s first item is a correctness fix and is not gated on anything;
its remaining storage phases are independent of P1 through P6, and its final
embedding-selection phase is gated on P1's evaluation harness.

The next trust release ships only when all of the following are true:

1. P1 and P2 exit criteria are met.
2. The full test suite, including the server-extra API contract suite, passes.
3. `knowcode doctor --mcp` passes for the supported local configuration.
4. Freshness and language-coverage checks report no unresolved correctness
   warnings for the target repository.
5. No `Critical` item is open in the [engineering backlog](engineering/backlog.md).
   None is open today. (This line named `BL-1` long after Phase B fixed it on
   2026-08-29; check the backlog rather than trusting the name here.)

P3 through P7 improve efficiency, adoption, and footprint, but they are not
permitted to weaken these release gates.

## Out of Scope

- A shared or hosted multi-tenant knowledge service.
- Making the HTTP gateway the primary path for this roadmap.
- Automatically rewriting agent configuration without an explicit user command.
- Declaring compatibility for an agent consumer before its current setup and
  invocation behavior are verified.
- Cost optimization that bypasses the retrieval and routing-quality gates.
