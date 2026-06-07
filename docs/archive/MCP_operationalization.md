# MCP Operationalization Plan

> [!NOTE]
> **Status: Completed (v1.1)**
> All phases (PR1 through PR7) of this operationalization plan have been fully implemented, verified, and merged.
> - **PR1 (Correctness Baseline)**: Correctness evaluations and deterministic tests are active.
> - **PR2 (Canonical MCP Contract)**: Canonical MCP contract aligned across all documentation.
> - **PR4 (Code discovery)**: Code discovery coverage expanded to include `.jsx`, `.tsx`, `.rs`, and `.vue` extensions.
> - **PR3 (Freshness & Safety)**: Freshness safety and staleness checks operationalized with watch mode delete/rename support.
> - **PR5 (Doctor flow)**: CLI `doctor` enhanced with checklist diagnostics.
> - **PR6 (Observability)**: Non-blocking local telemetry logging implemented.
> - **PR7 (Hardening Pass)**: Token budget defaults hardened to 4000 max tokens and JSON compacted.

## Completion Summary

| PR | Name | Status |
|----|------|--------|
| PR1 | Correctness Baseline | ✅ Complete |
| PR2 | Canonical MCP Contract | ✅ Complete |
| PR3 | Freshness & Stale-Context Safety | ✅ Complete |
| PR4 | Code Coverage Completeness | ✅ Complete |
| PR5 | Repo Bootstrap & Doctor Flow | ✅ Complete |
| PR6 | MCP Observability | ✅ Complete |
| PR7 | Hardening Pass | ✅ Complete |

The detailed briefs for each PR are preserved below as historical reference.


**Phased Plan**
1. `PR1 - Correctness Baseline`
Goal: make retrieval quality measurable before we change behavior. Build a checked-in golden query set for this repo and 1-2 real target repos, wire it into [scripts/evaluate.py](/Users/deepg/Desktop/KnowCode/scripts/evaluate.py:13), and add pass/fail thresholds for retrieval accuracy. Exit criteria: every change to retrieval can be scored against known-good questions, and regressions are visible before release.

2. `PR2 - Canonical MCP Contract`
Goal: make every MCP-based agent use one correct operating policy. Unify the repo rule in [.agent/rules/context.md](/Users/deepg/Desktop/KnowCode/.agent/rules/context.md:1), the setup guide in [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:61), and the README flow in [README.md](/Users/deepg/Desktop/KnowCode/README.md:262) into one source of truth. The rule should be: always call `retrieve_context_for_query` first, start with `verbosity="minimal"`, escalate to `standard` or `verbose` only when needed, and tune the sufficiency threshold from eval data rather than opinion. Exit criteria: one documented MCP policy, no threshold conflicts, and tests covering local-answer vs escalation behavior.

3. `PR3 - Freshness and Stale-Context Safety`
Goal: make agents trust that KnowCode reflects the current repo. The current watch path in [src/knowcode/api/main.py](/Users/deepg/Desktop/KnowCode/src/knowcode/api/main.py:11), [src/knowcode/indexing/background_indexer.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/background_indexer.py:11), and [src/knowcode/indexing/monitor.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/monitor.py:19) does not yet prove durable, correct updates for rename/delete/reload cases. Either harden watch mode so it persists and reloads safely, or downgrade it operationally and add explicit stale-artifact warnings plus a required refresh path. Exit criteria: edit/create/delete/rename scenarios are tested, and KnowCode either updates correctly or clearly refuses to answer from stale artifacts.

4. `PR4 - Code Coverage Completeness`
Goal: eliminate obvious false negatives from missing code ingestion. Fix scanner/parser mismatches such as `.jsx` and `.tsx` being handled in [src/knowcode/indexing/graph_builder.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/graph_builder.py:97) but not scanned in [src/knowcode/indexing/scanner.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/scanner.py:25). Then audit the actual languages used in the repos you want this on and produce a supported-language matrix. Exit criteria: KnowCode reports unsupported code types explicitly, and target repos do not silently lose major portions of code context.

5. `PR5 - Repo Bootstrap and Doctor Flow`
Goal: make day-to-day use one repeatable local-repo workflow instead of tribal knowledge. Add a bootstrap/doctor command that checks store existence, index existence, freshness, MCP readiness, active rules, and unsupported extensions, then prints an actionable status. Exit criteria: a new local repo can be brought to “KnowCode ready” with one command and a deterministic checklist, rather than manual docs-following.

6. `PR6 - MCP Observability for Correctness`
Goal: close the loop on whether the agent should keep trusting KnowCode. Log query, verbosity, sufficiency score, artifact age, whether the answer stayed local or escalated, and user-marked misses. Keep this local per repo since that matches your scope. Exit criteria: you can inspect correctness trends over time and tune thresholds, prompts, and freshness behavior with evidence.

7. `PR7 - Hardening Pass`
Goal: only after the first six are stable, tighten response shaping and operational polish. This includes retrieval contract cleanups, token-budget correctness, and any low-risk efficiency wins. Exit criteria: no open correctness-critical issues remain, and KnowCode is boring to operate day to day.

**Execution Order**
Do `PR1` and `PR2` first. `PR3` comes next because freshness is a correctness blocker. `PR4` can run partly in parallel once the eval harness exists. `PR5` should land after `PR2` to `PR4` stabilize. `PR6` starts lightweight after `PR2`, then expands after `PR3`. `PR7` is last.

**Release 1 Principle**
For the first release, correctness is the ship gate. Lower external-token spend matters, but it is not a release-blocking metric yet. The design should still preserve a clean path to cost optimization later, which means the first release should keep retrieval policy, payload shaping, thresholds, and telemetry extensible rather than hardwiring assumptions that would force a rewrite when spend becomes a first-class optimization target.

**Design Guardrails for Release 1**
1. Keep correctness-critical decisioning separate from cost-oriented payload shaping. Freshness safety, coverage completeness, and answer gating should not depend on later token-saving heuristics.
2. Keep verbosity, entity limits, and sufficiency thresholds configurable in code and config, not duplicated as fixed policy across multiple docs or prompts.
3. Make eval and telemetry schemas append-only or otherwise easy to extend so later spend metrics can be added without invalidating the correctness baseline.
4. Preserve a stable MCP contract that can support slimmer default responses later, without forcing immediate client breakage in the correctness-first release.
5. Prefer local-first control points that can later become cost-aware, but do not make spend optimization a hidden requirement for the first release.

**Out of Scope for This Phase**
Keep the HTTP gateway path out of this plan since you chose MCP-only. Keep non-code repository context out too, but record it as a future roadmap item after code-context correctness is reliable. Keep team-shared or remote deployment out as well since this is local-per-repo for now. Also keep direct optimization of external-token spend out of the release gate for this phase; instead, make design choices that leave a low-risk path to add cost metrics and cost tuning later.

**Definition of Done**
KnowCode is operationalized for release one when a local repo can be bootstrapped in one step, retrieval quality is measured on golden queries, stale artifacts are either automatically corrected or blocked, supported code coverage is explicit, and MCP agents follow one canonical retrieval policy with correctness-backed thresholds. External-token spend should be observable enough to optimize later, but it is not part of release-one ship criteria.

**Future Roadmap**
1. `Phase 1 - Correctness Release`
Ship the first release when correctness, freshness, coverage, bootstrap, and contract consistency are stable. Treat external-token spend as a non-gating observation, not a launch target.

2. `Phase 2 - Cost Visibility`
After release-one correctness stabilizes, extend the eval and telemetry surfaces to measure local-answer rate, escalation rate, payload size by verbosity, and estimated external-token usage. The purpose of this phase is to understand where spend is coming from without yet changing the correctness contract.

3. `Phase 3 - Cost-Aware Tuning`
Once the data is trustworthy, tighten default budgets, slim default responses, and tune the local-first ladder to lower external-token usage while preserving the correctness floor established in `PR1`. This is where payload-shaping work from [docs/mcp-contract.md](/Users/deepg/Desktop/KnowCode/docs/mcp-contract.md#appendix-token-overhead-reduction-strategies) should be prioritized based on evidence rather than intuition.

4. `Phase 4 - Advanced Cost Optimization`
Only after the earlier phases are stable should KnowCode consider more invasive moves such as tool-surface consolidation, more aggressive summary-first retrieval, or adaptive cost-aware escalation policies. Each such change should be justified against the golden-query baseline and the observed spend data, not adopted as a first-release architectural dependency.

If you want, I can turn this into a PR-by-PR implementation brief with file targets, test additions, and suggested acceptance tests for each PR.

**PR1 Brief**

This PR should establish a correctness baseline for KnowCode’s MCP retrieval path without changing production behavior yet. The goal is to make retrieval quality measurable and repeatable before we touch freshness, thresholds, or broader rollout policy.

**Context**
The repo already has the core retrieval and agent path in [src/knowcode/service.py](/Users/deepg/Desktop/KnowCode/src/knowcode/service.py:172), [src/knowcode/mcp/server.py](/Users/deepg/Desktop/KnowCode/src/knowcode/mcp/server.py:108), [tests/unit/service/test_retrieve_context_for_query.py](/Users/deepg/Desktop/KnowCode/tests/unit/service/test_retrieve_context_for_query.py:1), and [tests/e2e/test_usecase2_ide_integration.py](/Users/deepg/Desktop/KnowCode/tests/e2e/test_usecase2_ide_integration.py:1). What’s missing is a checked-in retrieval eval baseline. The repo itself calls out “retrieval golden-query tests” as missing hardening work in [README.md](/Users/deepg/Desktop/KnowCode/README.md:436).

**Objective**
Create a CI-safe, deterministic retrieval eval harness for code-only MCP usage, plus a local manual benchmark path for real-repo measurement.

**Scope**
1. Add a checked-in golden-query dataset for a small synthetic code fixture under `tests/eval/`.
2. Add deterministic pytest coverage that exercises the real retrieval stack with mock embeddings.
3. Upgrade `scripts/evaluate.py` so it can be used as a local measurement tool, not just an ad hoc script.
4. Document how to run and extend the eval set.
5. Keep the eval format extensible so future spend-oriented metrics can be added without replacing the baseline.
6. Keep production behavior unchanged in this PR.

**Files To Add**
- `tests/eval/data/retrieval_golden_queries.json` (harness structure is present in `tests/eval/harness/`, but golden datasets are still missing)
- `tests/eval/fixtures/mini_repo/` with a tiny multi-file code fixture
- `docs/retrieval-evals.md`

**Files To Update**
- `tests/eval/harness/test_retrieval_quality.py`
- `tests/eval/harness/scorer.py`
- [scripts/evaluate.py](/Users/deepg/Desktop/KnowCode/scripts/evaluate.py:1)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:436)
- Optionally [docs/architecture/reference_architecture.md](/Users/deepg/Desktop/KnowCode/docs/architecture/reference_architecture.md:103) if you want the roadmap item to point at the new harness

**Implementation Tasks**
1. Build a tiny fixture repo that covers the MCP use cases you care about most now:
   `locate`, `explain`, `debug`, and dependency tracing.
2. Define a simple dataset schema:
   `query`, `task_type`, `expected_entity_ids` (proposed schema field, implemented in `tests/eval/harness/scorer.py`), and optional `notes`.
3. Write deterministic pytest evals that use the real `Indexer`, `HybridIndex`, `SearchEngine`, and `KnowCodeService.retrieve_context_for_query`, but swap in a mock embedding provider so CI does not require API keys.
4. Assert on outcomes that matter for correctness:
   returned entity IDs, retrieval mode behavior, non-empty context where expected, and sufficiency score shape.
5. Refactor `scripts/evaluate.py` so it can:
   load the dataset,
   emit machine-readable JSON,
   optionally fail on caller-provided thresholds,
   remain manual-only when real embedding APIs are used.
6. Document the workflow:
   how to add a new query,
   how to run the deterministic suite,
   how to run the local benchmark against a real indexed repo.
7. Keep the output schema easy to extend with future fields such as payload-size or estimated-spend measurements, but do not make those metrics gating in this PR.

**Acceptance Criteria**
- `pytest` can run the new eval suite with no external API keys.
- The starter dataset covers at least one query for each current MCP-oriented task shape: locate, explain, debug, and dependency traversal.
- `scripts/evaluate.py` produces stable JSON output and can be used locally to record a baseline.
- The docs tell a contributor exactly how to extend the dataset.
- No MCP contract, threshold, or retrieval behavior changes land in this PR.

**Verification**
```bash
uv run pytest tests/eval tests/unit/service/test_retrieve_context_for_query.py tests/e2e/test_usecase2_ide_integration.py
uv run python scripts/evaluate.py tests/eval/data/retrieval_golden_queries.json knowcode_index
```

**Out Of Scope**
- Freshness/watch-mode fixes
- Threshold tuning
- MCP rule changes
- HTTP gateway/OpenAPI path
- Non-code repository context

**Suggested Review Focus**
- Are the eval fixtures representative enough to catch ranking/selection regressions?
- Is the dataset format easy to extend?
- Does the harness stay deterministic across platforms, given the CI matrix in [.github/workflows/ci-cd.yml](/Users/deepg/Desktop/KnowCode/.github/workflows/ci-cd.yml:1)?

**PR2 Brief**

This PR should turn the MCP retrieval path from a set of similar-but-conflicting instructions into one explicit operating contract. The main job here is not to improve ranking yet. It is to remove ambiguity so docs, rules, and runtime behavior all tell the agent to do the same thing.

**Context**
The repo already contains the shape of the desired contract, but it is split across conflicting locations. [.agent/rules/context.md](/Users/deepg/Desktop/KnowCode/.agent/rules/context.md:1) already says to start with `retrieve_context_for_query` in `verbosity="minimal"` and escalate only when the reduced bundle hides needed detail. Meanwhile [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:24) and [README.md](/Users/deepg/Desktop/KnowCode/README.md:262) still describe older threshold-driven flows that jump directly from a single retrieval call to an external LLM. Runtime threshold ownership is also split between [src/knowcode/config.py](/Users/deepg/Desktop/KnowCode/src/knowcode/config.py:23) and [src/knowcode/llm/agent.py](/Users/deepg/Desktop/KnowCode/src/knowcode/llm/agent.py:162).

**Objective**
Define one canonical MCP retrieval contract and make every agent-facing rule, guide, and local-first code path point at it.

**Scope**
1. Create one source of truth for the MCP retrieval policy.
2. Standardize the retrieval ladder: `minimal` first, then `standard` or `verbose` only when needed.
3. Clarify where the sufficiency threshold lives and how it should be tuned after `PR1`.
4. Add tests that prove the local-answer vs escalation behavior.
5. Keep verbosity and budget controls configurable so later spend tuning can happen without reworking the contract.
6. Keep freshness, coverage, and observability changes out of this PR.

**Files To Add**
- None (Note: `docs/mcp-contract.md` has been successfully implemented and is now the canonical contract)

**Files To Update**
- `docs/mcp-contract.md`
- [.agent/rules/context.md](/Users/deepg/Desktop/KnowCode/.agent/rules/context.md:1)
- [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:24)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:262)
- [src/knowcode/llm/agent.py](/Users/deepg/Desktop/KnowCode/src/knowcode/llm/agent.py:162)
- [src/knowcode/retrieval/orchestrator.py](/Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py:222)
- [tests/e2e/test_usecase2_ide_integration.py](/Users/deepg/Desktop/KnowCode/tests/e2e/test_usecase2_ide_integration.py:1)
- [tests/unit/mcp/test_mcp_server_tools.py](/Users/deepg/Desktop/KnowCode/tests/unit/mcp/test_mcp_server_tools.py:1)
- [tests/unit/llm/test_agent.py](/Users/deepg/Desktop/KnowCode/tests/unit/llm/test_agent.py:1)

**Implementation Tasks**
1. Write a short canonical contract doc that answers four questions clearly:
   which tool to call first,
   what default verbosity to use,
   when to escalate verbosity,
   where the local-answer threshold is configured.
2. Remove threshold conflicts from setup docs and README. The docs should reference a configured threshold and a recommended starting value, not repeat inconsistent hard-coded numbers in multiple places.
3. Align the agent wrapper with the documented retrieval ladder so it does not contradict the rule file when `minimal` mode hides the detail needed to answer safely.
4. Keep `diagnostic` mode as a testing and debugging surface, but make it clear that it is not the default operating policy for IDE agents.
5. Add tests that cover:
   default `minimal` retrieval,
   escalation when the reduced response is insufficient,
   local answer vs fallback behavior at the configured threshold.
6. Make sure the contract leaves room for later budget tightening and slimmer minimal responses without requiring another documentation fork.

**Acceptance Criteria**
- There is one canonical contract doc, and the rule file, setup guide, and README all point to it rather than redefining policy separately.
- Conflicting threshold guidance is resolved (standardized to `0.8` default in config and contract, while unit tests use `0.75` for isolated testing scenarios without conflict).
- The local-first runtime path follows the same `minimal` then escalate behavior described in the docs.
- Tests cover both local-answer and escalation paths.

**Verification**
```bash
uv run pytest tests/unit/mcp/test_mcp_server_tools.py tests/unit/llm/test_agent.py tests/e2e/test_usecase2_ide_integration.py
rg -n "0\\.75|0\\.8|0\\.88|verbosity|retrieve_context_for_query" README.md docs/MCP_SETUP.md .agent/rules/context.md docs/mcp-contract.md
```

**Out Of Scope**
- Freshness/watch-mode durability fixes
- Supported-language expansion beyond contract wording
- Query telemetry and feedback logging
- HTTP gateway behavior

**Suggested Review Focus**
- Does the contract actually remove ambiguity, or just restate it in one more place?
- Is threshold ownership clear enough that future tuning happens in config and evals rather than docs?
- Does the runtime path really honor the same escalation rules the docs now describe?

**PR3 Brief**

This PR should make freshness an explicit safety property instead of a hopeful assumption. The current watch mode is promising, but it does not yet prove that KnowCode stays correct across file deletion, rename, and long-running server sessions.

**Context**
Watch mode exists today through [src/knowcode/api/main.py](/Users/deepg/Desktop/KnowCode/src/knowcode/api/main.py:11), [src/knowcode/indexing/background_indexer.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/background_indexer.py:11), and [src/knowcode/indexing/monitor.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/monitor.py:19). But the monitor currently reacts only to create and modify events, filters extensions independently of the scanner, and queues only `index_file()` work. It does not yet cover delete or move semantics, and it does not show a durable freshness contract for persisted artifacts. The only clearly tested refresh path today is the manual reload endpoint in [tests/e2e/test_server_refinement.py](/Users/deepg/Desktop/KnowCode/tests/e2e/test_server_refinement.py:1).

**Objective**
Ensure KnowCode either keeps artifacts fresh enough to trust or clearly tells the caller that local context may be stale.

**Scope**
1. Decide whether watch mode becomes a supported freshness mechanism or remains best-effort.
2. Handle modify, create, delete, and rename cases explicitly.
3. Add a freshness signal that can be surfaced to MCP and API callers.
4. Provide a deterministic manual recovery path when freshness cannot be guaranteed.
5. Keep observability and bootstrap UX mostly out of this PR, except where they depend on the freshness signal.

**Files To Add**
- `tests/unit/indexing/test_monitor.py`
- `tests/unit/service/test_freshness.py`

**Files To Update**
- [src/knowcode/api/main.py](/Users/deepg/Desktop/KnowCode/src/knowcode/api/main.py:11)
- [src/knowcode/indexing/background_indexer.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/background_indexer.py:11)
- [src/knowcode/indexing/monitor.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/monitor.py:19)
- [src/knowcode/service.py](/Users/deepg/Desktop/KnowCode/src/knowcode/service.py:108)
- [src/knowcode/api/api.py](/Users/deepg/Desktop/KnowCode/src/knowcode/api/api.py:222)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:166)
- [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:111)
- `tests/unit/indexing/test_background_indexer.py` (Note: already implemented)

**Implementation Tasks**
1. Choose the supported behavior up front:
   either watch mode updates persisted artifacts safely,
   or watch mode is marked best-effort and retrieval refuses confident local answers once artifacts are known stale.
2. Extend the monitor to handle `on_deleted` and `on_moved`, and keep its extension logic aligned with the scanner.
3. Teach the background worker how to represent removals and renames, not just re-index individual files.
4. Add freshness metadata that can answer basic operational questions:
   when the store was last rebuilt,
   when the index was last rebuilt,
   whether the source tree has changed since then.
5. Surface stale state as structured output instead of silent drift, so the caller can refresh with `knowcode analyze`, `knowcode index`, or `/reload`.
6. Add tests for modify, create, delete, and rename scenarios, including the stale-artifact fallback path.

**Acceptance Criteria**
- Modify, create, delete, and rename cases are covered by tests.
- KnowCode does not silently present known-stale artifacts as trustworthy context.
- There is a documented and testable recovery path when artifacts become stale.
- Watch mode behavior is described honestly as either supported or best-effort.

**Verification**
```bash
uv run pytest tests/unit/indexing/test_monitor.py tests/unit/indexing/test_background_indexer.py tests/unit/service/test_freshness.py tests/e2e/test_server_refinement.py
```

**Out Of Scope**
- Cross-machine sync
- Remote/shared artifact management
- Perfect real-time updates for every editor or filesystem edge case
- Telemetry dashboards

**Suggested Review Focus**
- Are stale artifacts ever still able to produce a confident local answer?
- Do delete and rename semantics actually remove or invalidate old entities/chunks?
- Is the supported watch-mode story honest enough for operators to trust?

**PR4 Brief**

This PR should remove silent ingestion gaps from the code-only MCP path. The immediate problem is not deep parser quality. It is the mismatch between what the scanner discovers, what the graph builder can parse, and what the docs say is supported.

**Context**
There is already a clear scanner/parser mismatch in [src/knowcode/indexing/scanner.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/scanner.py:25) versus [src/knowcode/indexing/graph_builder.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/graph_builder.py:97). The graph builder handles `.jsx` and `.tsx`, but the scanner does not currently discover those files. The README supported-language section also lags the implementation: [README.md](/Users/deepg/Desktop/KnowCode/README.md:289) lists Python, JavaScript/TypeScript, Java, Markdown, and YAML, while the codebase also includes parser support and tests for Rust and Vue.

**Objective**
Make supported-code coverage explicit and prevent major code categories from being silently omitted during indexing.

**Scope**
1. Align scanner discovery with parser support.
2. Publish an exact supported-extension matrix instead of broad language labels only.
3. Audit the real extensions used in the repos you plan to operationalize.
4. Surface unsupported or out-of-scope extensions clearly in docs and future doctor output.
5. Keep parser-quality upgrades themselves out of this PR unless they block coverage claims.

**Files To Add**
- `docs/supported-language-matrix.md`

**Files To Update**
- [src/knowcode/indexing/scanner.py](/Users/deepg/Desktop/KnowCode/src/knowcode/indexing/scanner.py:25)
- [tests/unit/indexing/test_scanner.py](/Users/deepg/Desktop/KnowCode/tests/unit/indexing/test_scanner.py:1)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:289)
- [docs/index.md](/Users/deepg/Desktop/KnowCode/docs/index.md:235)
- [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:1)

**Implementation Tasks**
1. Fix the immediate discovery mismatch so the scanner and graph builder agree on `.jsx` and `.tsx`.
2. Extend scanner tests to cover the full claimed extension set, including `.rs`, `.vue`, `.jsx`, and `.tsx`.
3. Publish a support matrix that distinguishes:
   discovered and parsed,
   parsed but still experimental,
   not supported and therefore excluded.
4. Audit the extensions present in the actual target repos and record which ones are fully covered versus currently out of scope.
5. Add explicit language in the docs that unsupported file types are skipped by design rather than silently assumed to work.

**Acceptance Criteria**
- Every extension claimed as supported in docs is actually discovered by the scanner and routed to a parser.
- The docs list exact extensions, not just high-level language names.
- Target repos have an explicit extension audit so you know what KnowCode will miss before rollout.
- Unsupported extensions are surfaced as a limitation rather than a silent false negative.

**Verification**
```bash
uv run pytest tests/unit/indexing/test_scanner.py tests/unit/parsers/test_rust_parser.py tests/unit/parsers/test_vue_parser.py
```

**Out Of Scope**
- New parser implementations for additional languages
- Parser-depth improvements for already supported languages
- Non-code repository context

**Suggested Review Focus**
- Does the scanner now cover every extension the graph builder can parse?
- Are the docs honest about what is and is not supported?
- Would a user operationalizing this on a React, Vue, or mixed-language repo spot missing coverage before trusting the answers?

**PR5 Brief**

This PR operationalizes everyday setup checks. Note: The `knowcode doctor` command has been fully implemented, and a separate `bootstrap` command is superseded by `knowcode build` + `knowcode doctor`.

**Context**
The CLI already exposes the primitives in [src/knowcode/cli/cli.py](/Users/deepg/Desktop/KnowCode/src/knowcode/cli/cli.py:18): `analyze`, `index`, `build`, `doctor`, `server`, `mcp-server`, `stats`, and `ask`. The doctor check is backed by `src/knowcode/doctor.py` and tested under `tests/unit/cli/test_doctor.py`.

**Objective**
Ensure the bootstrap and verification workflow using `knowcode build` and `knowcode doctor` gets a repo to “KnowCode ready” without relying on tribal knowledge.

**Scope**
1. Document the setup flow using `knowcode build` to bootstrap and `knowcode doctor` to verify.
2. Refine the doctor flow to report readiness and next steps deterministically using freshness and coverage signals.
3. Replace machine-specific verification guidance in `verify_mcp_connection.sh` with portable check logic.
4. Update setup docs so the happy path is command-first.

**Files To Add**
- None (All required tools and tests are already created)

**Files To Update**
- [src/knowcode/cli/cli.py](/Users/deepg/Desktop/KnowCode/src/knowcode/cli/cli.py:18)
- [src/knowcode/service.py](/Users/deepg/Desktop/KnowCode/src/knowcode/service.py:90)
- [tests/unit/cli/test_cli.py](/Users/deepg/Desktop/KnowCode/tests/unit/cli/test_cli.py:1)
- [tests/unit/cli/test_doctor.py](/Users/deepg/Desktop/KnowCode/tests/unit/cli/test_doctor.py)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:248)
- [docs/MCP_SETUP.md](/Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md:40)
- [verify_mcp_connection.sh](/Users/deepg/Desktop/KnowCode/verify_mcp_connection.sh:1)

**Implementation Tasks**
1. Ensure the doctor report has clear pass/warn/fail output for:
   knowledge store presence,
   semantic index presence,
   freshness state,
   MCP server readiness,
   active rule file presence,
   unsupported-extension warnings.
2. Rely on the `build` command (which internally calls `ensure_store` and `ensure_index` behavior) to initialize repository artifacts instead of adding a separate bootstrap command.
3. Make verification logic path-agnostic so it works outside the original `/home/deeog/...` environment.
4. Rewrite setup docs around the build/doctor flow and demote manual checklists to fallback documentation.

**Acceptance Criteria**
- A new local repo can be initialized and verified using `knowcode build` and `knowcode doctor`.
- The doctor command explains exactly what is missing or stale and what the next fix step is.
- Verification no longer depends on hardcoded absolute paths to one user’s machine or one MCP client.
- CLI tests cover common build and doctor prerequisite-failure cases.

**Verification**
```bash
uv run pytest tests/unit/cli/test_cli.py tests/unit/cli/test_doctor.py
uv run knowcode build . --output .
uv run knowcode doctor --store .
```

**Out Of Scope**
- Shared or remote deployment workflows
- Automatic IDE reconfiguration for every client
- Observability dashboards beyond readiness checks

**Suggested Review Focus**
- Does the doctor workflow reuse existing service behavior cleanly?
- Is the doctor output deterministic and actionable, or does it still require reading multiple docs?
- Have the hardcoded environment assumptions been removed?

**PR6 Brief**

This PR should add just enough local observability to tune correctness with evidence. The point is not a hosted analytics system. It is a per-repo trail that tells you whether KnowCode is staying trustworthy over time, with a schema that can later accommodate spend-oriented metrics without replacing the correctness-first pipeline.

**Context**
The key fields already exist in the runtime path: query text, verbosity, sufficiency score, retrieval mode, and local-vs-LLM decisioning all appear in [src/knowcode/retrieval/orchestrator.py](/Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py:202), [src/knowcode/mcp/server.py](/Users/deepg/Desktop/KnowCode/src/knowcode/mcp/server.py:109), and [src/knowcode/llm/agent.py](/Users/deepg/Desktop/KnowCode/src/knowcode/llm/agent.py:162). The architectural recommendation already points in this direction in [docs/architecture/json-storage-analysis.md](/Users/deepg/Desktop/KnowCode/docs/architecture/json-storage-analysis.md:403), but there is no local correctness log yet.

**Objective**
Record enough local correctness and freshness telemetry per repo to tune thresholds, detect regressions, and capture misses without introducing remote infrastructure. The schema should also be easy to extend later with cost-oriented fields, but those are not release-one gates.

**Scope**
1. Define a local per-repo telemetry schema.
2. Log retrieval and answer-decision events on the MCP/local-first path.
3. Include freshness context so misses can be tied back to stale artifacts.
4. Add a lightweight way to record user-marked misses.
5. Keep storage simple and failure-tolerant.

**Files To Add**
- `src/knowcode/telemetry.py`
- `tests/unit/test_telemetry.py`
- `docs/observability.md`

**Files To Update**
- [src/knowcode/retrieval/orchestrator.py](/Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py:202)
- [src/knowcode/mcp/server.py](/Users/deepg/Desktop/KnowCode/src/knowcode/mcp/server.py:109)
- [src/knowcode/llm/agent.py](/Users/deepg/Desktop/KnowCode/src/knowcode/llm/agent.py:162)
- [src/knowcode/service.py](/Users/deepg/Desktop/KnowCode/src/knowcode/service.py:430)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:262)
- [docs/architecture/json-storage-analysis.md](/Users/deepg/Desktop/KnowCode/docs/architecture/json-storage-analysis.md:403)

**Implementation Tasks**
1. Define an append-only local log schema that captures at least:
   timestamp,
   query,
   verbosity,
   sufficiency score,
   artifact age or freshness state,
   whether the answer stayed local or escalated,
   whether the user later marked it as a miss.
2. Store telemetry per repo rather than globally, so it reflects the operational scope you chose.
3. Log events in a way that never blocks answering. If telemetry writes fail, retrieval should still work.
4. Add a lightweight summary or inspection path so you can review correctness trends without parsing raw files by hand.
5. Leave room in the schema for future fields such as payload size, escalation counts, or estimated external-token usage, without requiring those fields to be present in the first release.
6. Document retention, privacy tradeoffs, and how this data feeds future threshold tuning and later spend optimization.

**Acceptance Criteria**
- Every MCP/local-first query can produce a local telemetry record without requiring remote services.
- The record includes enough data to distinguish a ranking miss from a stale-artifact miss.
- Logging failures do not break retrieval or answering.
- There is a documented way to inspect trends and record user-marked misses.

**Verification**
```bash
uv run pytest tests/unit/test_telemetry.py tests/unit/mcp/test_mcp_server_tools.py tests/unit/llm/test_agent.py
```

**Out Of Scope**
- Hosted observability platforms
- Team-shared analytics aggregation
- Fine-grained UX or dashboard work

**Suggested Review Focus**
- Is the schema sufficient to explain future false positives and false negatives?
- Does the log stay local to the repo as intended?
- Are telemetry failures safely isolated from the main retrieval path?

**PR7 Brief**

This PR should be the cleanup and polish pass after the correctness-critical work is already stable. It is where you tighten payload shape, token budgets, and other low-risk operational details without reopening the earlier contract questions. It should prepare the path for later spend optimization, but not turn spend reduction into a competing ship gate for the first release.

**Context**
The repo already has a concrete list of payload-shaping opportunities in [docs/mcp-contract.md](/Users/deepg/Desktop/KnowCode/docs/mcp-contract.md#appendix-token-overhead-reduction-strategies). The retrieval path in [src/knowcode/retrieval/orchestrator.py](/Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py:202) already gates fields by verbosity, and the MCP server response surface is tested in [tests/unit/service/test_retrieve_context_for_query.py](/Users/deepg/Desktop/KnowCode/tests/unit/service/test_retrieve_context_for_query.py:1) and [tests/unit/mcp/test_mcp_server_tools.py](/Users/deepg/Desktop/KnowCode/tests/unit/mcp/test_mcp_server_tools.py:1). That makes this a good last-mile hardening pass once the earlier PRs have reduced correctness risk.

**Objective**
Deliver low-risk response-shaping and operational cleanup only after correctness, freshness, coverage, bootstrap, and observability are in place, while preserving the evidence-backed path to later spend tuning.

**Scope**
1. Tighten default payload size and response shape where measurements justify it.
2. Remove or gate low-signal metadata from the default MCP path.
3. Revisit token-budget defaults using evidence from `PR1` and `PR6`.
4. Clean up remaining doc/runtime drift.
5. Avoid semantic behavior changes that would need another large rollout.

**Files To Update**
- [src/knowcode/mcp/server.py](/Users/deepg/Desktop/KnowCode/src/knowcode/mcp/server.py:109)
- [src/knowcode/retrieval/orchestrator.py](/Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py:202)
- [docs/mcp-contract.md](/Users/deepg/Desktop/KnowCode/docs/mcp-contract.md#appendix-token-overhead-reduction-strategies)
- [README.md](/Users/deepg/Desktop/KnowCode/README.md:262)
- [tests/unit/service/test_retrieve_context_for_query.py](/Users/deepg/Desktop/KnowCode/tests/unit/service/test_retrieve_context_for_query.py:1)
- [tests/unit/mcp/test_mcp_server_tools.py](/Users/deepg/Desktop/KnowCode/tests/unit/mcp/test_mcp_server_tools.py:1)

**Implementation Tasks**
1. Use the eval and telemetry evidence from earlier PRs to pick conservative default budgets rather than guessing.
2. Keep the minimal response minimal. Move anything nonessential behind `standard`, `verbose`, or `diagnostic` if downstream callers do not need it by default.
3. Compact serialization and response shaping only after tests confirm MCP clients still behave correctly.
4. Audit the docs for leftover contract drift after the earlier PRs land.
5. Run the full correctness suite before merge so “hardening” does not quietly reintroduce regressions.

**Acceptance Criteria**
- Default MCP payload size decreases without reducing correctness on the golden-query suite.
- The response contract is still clear and test-covered after any shaping changes.
- No correctness-critical issues remain open from the first six PRs.
- The product feels operationally boring in the common local-repo workflow.

**Verification**
```bash
uv run pytest tests/eval tests/unit/service/test_retrieve_context_for_query.py tests/unit/mcp/test_mcp_server_tools.py tests/unit/llm/test_agent.py
```

**Out Of Scope**
- New retrieval features
- Large schema or storage redesigns
- Remote deployment concerns

**Suggested Review Focus**
- Are the payload reductions backed by evidence, not instinct?
- Did any hardening change alter the behavior of the canonical MCP contract?
- Does the final default path feel simpler without becoming less safe?

If you want, I can turn this full plan into a condensed implementation checklist next, or start on whichever PR you want to execute first.
