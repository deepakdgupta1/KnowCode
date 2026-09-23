# KnowCode Documentation Refresh — Multi-Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring every living markdown document in the repository back in sync with the code as of 2026-09-22 (package version 0.2.3), without touching frozen records.

**Architecture:** Eight small phases, each independently shippable and gated by the repo's own docs tooling (`scripts/check_doc_links.py` + non-strict `mkdocs build`). Every fix below was verified against `src/` with file:line citations on 2026-09-22; edits are specified as exact quote-anchored replacements so an engineer with zero context can execute them.

**Tech Stack:** Markdown + mkdocs (material, mkdocstrings), Python 3.10–3.12, uv, pytest, ruff, mypy (strict).

## Global Constraints

Every task implicitly includes this section.

- **Verification commands** (run from repo root):
  - `uv run python scripts/check_doc_links.py` — resolves every relative Markdown link in `README.md`, `CONTRIBUTING.md`, and `docs/**` except `docs/archive/`. Must exit 0 (~198 links resolve today).
  - `uv run mkdocs build` — must succeed. **Never `--strict`**: docs legitimately link into `src/`, and CI deliberately builds non-strict (`.github/workflows/ci-cd.yml:77-84`).
  - `uv run pytest -q` — must stay green; this plan edits docs only, but several tests pin doc facts (e.g. `tests/unit/docs/test_storage_plan_anchors.py`).
- **Frozen — never edit:** `docs/archive/**` ("never edit archived documents", `CONTRIBUTING.md:58-59`), `tests/test_sample-prose/**` (test fixtures), `site/**` (build output), `docs/research/gpt-actions-rag/**` (vendored clone, gitignored per `.gitignore:230`).
- **ADR convention:** ADRs are append-only. Superseded text gains a dated `Update (YYYY-MM-DD)` note with a pointer; decision text itself is never rewritten (convention stated in `docs/engineering/adr/index.md`). ADRs 0001–0007, 0009, 0010 were audited and match code — do not touch them.
- **Working tree precondition:** as of 2026-09-22 the tree carries an uncommitted BL-37 fix (`docs/engineering/backlog.md`, `docs/user/configuration.md`, `src/knowcode/llm/embedding.py`, `src/knowcode/llm/routing.py`, `tests/unit/llm/test_embedding_provider.py`). Phase 0 lands it before any doc edits.
- **Quote anchors rot:** every "Current:" quote below was accurate on 2026-09-22. If a quote no longer matches the file, re-verify the fact against the cited `src/` location before editing — do not blind-edit.
- **Commit style:** one commit per task, conventional format like the existing log (`docs(user): ...`, `docs(engineering): ...`, `chore: ...`).
- **Parallel execution rule:** implementer subagents edit files and run read-only verification only — they **never run git commands**. The controller stages and commits per task (task file sets are disjoint, so per-task commits stay clean).
- **Out of scope (flagged, not fixed here):** redrawing `.drawio`/`.svg` diagram bodies; the CI changelog job's mechanics beyond the one `.gitignore` line in Task 1.4; anything in `src/` besides zero lines (this plan is docs-only).

---

## Phase 0 — Baseline, in-flight work, and site hygiene

Small on purpose: land what's already in the tree, record a green baseline, and fix the one mkdocs config line that makes local and CI docs builds diverge.

### Task 0.1: Land the in-flight BL-37 fix

**Files:** `src/knowcode/llm/embedding.py`, `src/knowcode/llm/routing.py`, `tests/unit/llm/test_embedding_provider.py`, `docs/engineering/backlog.md`, `docs/user/configuration.md` (all already modified).

- [ ] **Step 1: Confirm the BL-37 state is coherent**

Run: `git status --short && git diff --stat`
Expected: exactly the five files above modified, nothing else.

- [ ] **Step 2: Run the pinning test and lint**

Run: `uv run pytest tests/unit/llm/test_embedding_provider.py -q && uv run ruff check src tests`
Expected: PASS, no lint errors.

- [ ] **Step 3: Commit**

```bash
git add src/knowcode/llm/embedding.py src/knowcode/llm/routing.py tests/unit/llm/test_embedding_provider.py docs/engineering/backlog.md docs/user/configuration.md
git commit -m "fix(llm): BL-37 — pin the embedding/routing default targets and close the backlog row"
```

### Task 0.2: Record a green docs baseline

- [ ] **Step 1: Run the link checker**

Run: `uv run python scripts/check_doc_links.py`
Expected: exit 0, reports ~198 resolved links. If it fails here, stop and fix the baseline first — later phases must not chase pre-existing breakage.

- [ ] **Step 2: Build the site (non-strict)**

Run: `uv run mkdocs build`
Expected: succeeds. Note any pre-existing warnings in the commit message of Task 0.3 so they aren't attributed to it.

### Task 0.3: mkdocs exclude hygiene

**Files:**
- Modify: `mkdocs.yml:7-8`

**Interfaces:** Produces — a `site/` and link-check corpus that is identical between a fresh CI checkout and a local working tree. All later phases depend on this.

- [ ] **Step 1: Replace the single exclude pattern**

Current:

```yaml
# Superseded docs are kept for history but excluded from the built site.
exclude_docs: archive/*
```

New:

```yaml
# Superseded docs are kept for history but excluded from the built site.
# gpt-actions-rag is a vendored, gitignored clone — excluding it keeps local
# builds identical to CI checkouts. superpowers/ holds working plans, not docs.
exclude_docs: |
  archive/*
  research/gpt-actions-rag/*
  superpowers/*
```

- [ ] **Step 2: Verify both builds now agree**

Run: `rm -rf site && uv run mkdocs build && test ! -e site/research/gpt-actions-rag && test ! -e site/superpowers && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add mkdocs.yml
git commit -m "chore(docs): exclude the vendored research clone and working plans from the site build"
```

---

## Phase 1 — Repo-root meta documents (README, CONTRIBUTING, CHANGELOG)

Three files that describe the project and its process. The changelog tracking question is decided here because README and CONTRIBUTING both link to a file that is currently gitignored (`.gitignore:214`; deleted from the repo in commit `82fcd14`), and the CI changelog job runs `git add CHANGELOG.md` (`.github/workflows/ci-cd.yml:121`), which fails on an ignored path.

### Task 1.1: README.md — build gate, Vue parser wording

**Files:**
- Modify: `README.md` (build/check instructions near line 117; language list near lines 91-92)

- [ ] **Step 1: Replace the strict-mode build instruction**

Find the fenced command block containing:

```
mkdocs build --strict  # docs site must build warning-free
```

Replace that line with the two commands CI actually runs:

```
uv run mkdocs build                        # docs site must build warning-free (strict mode is off: docs link into src/)
uv run python scripts/check_doc_links.py   # every relative docs link must resolve
```

- [ ] **Step 2: Align the Vue parser description**

Find the supported-languages bullet describing Vue as a "custom Vue SFC" parser and replace it with:

> Vue — a custom SFC scanner (`parsers/vue_parser.py`) that delegates embedded script blocks to the tree-sitter JS/TS parsers

This is the accurate description; `docs/engineering/architecture.md` is aligned to the same wording in Task 4.1.

- [ ] **Step 3: Remove the changelog link (Option B, decided 2026-09-22)**

`CHANGELOG.md` stays untracked (`.gitignore:214`), so the link is dead in any fresh clone. Delete the changelog link (the relative `CHANGELOG.md` link in the bottom links list) from the README entirely.

- [ ] **Step 4: Verify links and commit**

Run: `uv run python scripts/check_doc_links.py` (exit 0)

```bash
git add README.md
git commit -m "docs(readme): describe the real docs gate and the Vue scanner accurately"
```

### Task 1.2: CONTRIBUTING.md — CI gates and changelog process

**Files:**
- Modify: `CONTRIBUTING.md` (lines ~20-28 "CI runs the same gates"; lines ~35-37 changelog paragraph)

- [ ] **Step 1: Rewrite the gates list to match `ci-cd.yml`**

Current (abridged): a list claiming local gates are `ruff check src/`, `ruff format`, mypy, pytest, and `mkdocs build --strict`.

Replace with:

> CI (`.github/workflows/ci-cd.yml`) runs: `uv run ruff check .`, `uv run mypy src` (strict), `uv run pytest`, `uv run mkdocs build` (deliberately **not** `--strict` — docs link into `src/`), and `uv run python scripts/check_doc_links.py` (every relative docs link must resolve). There is no `ruff format` gate; match the existing style instead.

- [ ] **Step 2: Rewrite the changelog paragraph**

Current: "the changelog is generated from them (`scripts/generate_changelog.py`)".

Replace with (CHANGELOG.md is untracked — decided 2026-09-22, no re-tracking):

> `CHANGELOG.md` is intentionally kept out of version control; generate a local entry draft with `uv run python scripts/generate_changelog.py` and curate it by hand.

(The CI changelog job that runs `git add CHANGELOG.md` on an ignored path is a pre-existing inconsistency outside this plan's docs scope — flagged to the maintainer, not fixed here.)

- [ ] **Step 3: Verify and commit**

Run: `uv run python scripts/check_doc_links.py` (exit 0)

```bash
git add CONTRIBUTING.md
git commit -m "docs(contributing): state the CI gates and changelog flow as they actually run"
```

### Task 1.3: CHANGELOG.md — correct and complete the Unreleased section

**Files:**
- Modify: `CHANGELOG.md` (Unreleased section, ~lines 85-125)

- [ ] **Step 1: Fix the stale schema claim**

Find the Unreleased mention of "chunk schema v2" and change it to **v4** (`SqliteChunkRepository.SCHEMA_VERSION = 4`, `src/knowcode/storage/sqlite_chunk_repository.py:146`). If the entry text is specific (e.g. names a v2 migration), reword to describe schema v4: deflated `chunks.content`, `schema_meta` table, SQLite ≥ 3.43 floor.

- [ ] **Step 2: Add the missing shipped work**

Append to the appropriate Unreleased subsections (Added / Fixed), one line each, mirroring the file's existing tone:

> - Bind stated receiver types with repo knowledge; classify import-bound references; type module-scope, literal, and qualified-return receivers — the BL-31→BL-34 reference-resolution stream (`8572c33`, `ec330f2`, `d9021c0`, `8572c33`).
> - Preflight breaks unresolved evidence down by hole class (`979f399`).
> - Doctor steps over an embedding model this build cannot construct (BL-36, `6909cf7`).
> - CI resolves every relative documentation link (`7849e78`).
> - Roadmap split into a forward plan and a history record (`3c4043a`).

Plus, once Task 0.1 is merged, a BL-37 line matching that commit.

- [ ] **Step 3: Verify**

Run: `uv run python scripts/check_doc_links.py` (exit 0). No content check tooling exists for changelog prose — re-read the diff.

### Task 1.4: CHANGELOG tracking decision — resolved: keep untracked

Decided by the maintainer on 2026-09-22: **do not re-track CHANGELOG.md.** No `.gitignore` change, no `git add` of the file. The docs-side consequences are distributed: Task 1.1 Step 3 removes the README link, and Task 1.2 Step 2 rewrites CONTRIBUTING's changelog paragraph to describe the local-only flow. Nothing further to edit or commit in this task; recorded here so the decision survives in the plan.

---

## Phase 2 — The two canonical contracts (REST API, MCP)

Highest user impact. Both documents undersell or misstate behavior shipped in the last month (token budgets, response profiles, payload caps).

### Task 2.1: docs/user/rest-api.md — query parameters, truncation, 412, freshness truth

**Files:**
- Modify: `docs/user/rest-api.md` (endpoint tables ~lines 20-40; "Semantics worth knowing" ~lines 45-60; examples ~lines 60-79)

- [ ] **Step 1: Fix the freshness claim**

Current (lines ~53-56): "Retrieval responses carry the same block: stale results are flagged, not blocked."

Replace with:

> The REST retrieval responses carry **no** freshness block — only `GET /api/v1/freshness` reports staleness on this surface. (The MCP surface is different: `knowcode_retrieve` responses embed a `freshness` block.)

- [ ] **Step 2: Fix the "same pipeline as ask" row**

Current (line ~29): `POST /api/v1/context/query` "same pipeline as `knowcode ask`".

Replace with: "shares the retrieval orchestration with `knowcode ask`, but returns ranked chunks instead of the LLM answer bundle."

- [ ] **Step 3: Document the query-endpoint request contract**

Add a "Request parameters" subsection under `POST /api/v1/context/query` with this table (all values from `src/knowcode/api/api.py:120-131`):

| Field | Type | Default | Bounds | Notes |
|---|---|---|---|---|
| `query` | string | required | — | |
| `limit` | int | 5 | 1–20 | max chunks returned |
| `max_tokens` | int | 4000 | ≤ 8000 | hard token budget for the response |
| `expand_deps` | bool | true | — | pull in dependency context |
| `task_type` | enum | `general` | no `auto` on REST | one of explain/debug/extend/review/locate/general |

- [ ] **Step 4: Document truncation and 412**

Add two short paragraphs:

> **Token-budget truncation.** The response is capped to `max_tokens`; chunk contents are truncated with a `\n... [TRUNCATED]` marker (minimum 25 tokens of truncatable content is kept). The budget also flows into retrieval ranking (`api/api.py:196-207,263-282`).
>
> **Prerequisite failures return HTTP 412** with `{"message", "code", "hint"}` — e.g. a missing knowledge store or semantic index (`api/api.py:208-212`).

- [ ] **Step 5: Complete the other endpoints' parameters**

Add to the endpoint tables: `GET /search` `limit` (default 20, 1–50); `GET /context` `max_tokens` (default 2000, 100–8000); `GET /trace_calls` `direction` (default `callees`), `depth` (1–5, default 1), `max_results` (default 50, 1–100); `GET /impact` `max_depth` (default 3, 1–5). Sources: `api/api.py:295-464`.

- [ ] **Step 6: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run mkdocs build` (both succeed)

```bash
git add docs/user/rest-api.md
git commit -m "docs(rest): document query budgeting, truncation, 412s, and per-endpoint parameters"
```

### Task 2.2: docs/mcp-contract.md — minimal-response fields, 1500 default, batching, error catalog

**Files:**
- Modify: `docs/mcp-contract.md` (header line 3; readiness ~lines 53-54; strategy/status sections ~lines 225-306; error catalog section)

- [ ] **Step 1: Update the header**

Change `Last Updated: 2026-08-28` to the execution date. (The doc's own status section records work through 2026-09-09, so 08-28 is internally contradictory.)

- [ ] **Step 2: Fix the minimal-verbosity field list**

Current (Strategy 2 / Status v1.1 item 1, line ~298): "The default `minimal` verbosity mode now returns only `context_text`, `sufficiency_score`, and `total_tokens`."

Replace with:

> The default `minimal` mode returns `context_text`, `sufficiency_score`, and `total_tokens`, **plus**: the `freshness` block the service appends to every query response; `reduction_summary` when the response was summarized, or `source_included: true` when raw source was included (task types `debug`/`review`, or verbosity ≥ `standard`); and `errors` when present (`src/knowcode/retrieval/orchestrator.py:342-364`, `src/knowcode/service.py:643`). Strip-and-compact narratives must treat these fields as part of the minimal payload.

- [ ] **Step 3: Fix the max_tokens default claim**

Current (line ~232 and ~299): "default `max_tokens` has been reduced from `6000` to `4000` across `RetrievalOrchestrator` and `KnowCodeMCPServer`".

Replace with:

> `max_tokens` defaults: **1500** for `knowcode_retrieve action=query` (`mcp/server.py:381`); 4000 for direct `RetrievalOrchestrator` use, the legacy flat tool, and the REST query endpoint.

- [ ] **Step 4: Fix the embedding round-trip claim**

Current (Readiness, lines ~53-54): "indexing costs one embedding round-trip per file with no cross-file batching or concurrency".

Replace with:

> Indexing embeds pending chunks in cross-file batches with bounded concurrency and retry/backoff (`src/knowcode/indexing/embedding_batch.py`); large first builds still take a while, which is why lifecycle actions return a `job_id` immediately and are polled via `knowcode_inspect action=job_status`.

- [ ] **Step 5: Extend the error-code catalog**

Current: only `missing_knowledge_store` and `entity_not_found` documented. Add rows:

| Code | Raised by | Meaning |
|---|---|---|
| `missing_semantic_index` | `semantic_search` | no usable vector plane in the index generation |
| `path_outside_root` | root resolution | requested path escapes the MCP server root |
| `unknown_job` / `no_jobs` | `inspect action=job_status` | job id not found / no jobs recorded yet |
| `missing_preflight_report` | `inspect action=preflight` | generation has no `preflight_report.json` |
| `job_already_running` | `knowcode_lifecycle` | one lifecycle job at a time; submit again after it finishes |

Sources: `src/knowcode/errors.py:35`, `mcp/roots.py:41-59`, `mcp/inspection.py:47-60,110-118`, `mcp/jobs.py:57-66`.

- [ ] **Step 6: Add the export requirement and single-job rule to the lifecycle section**

Two sentences: `knowcode_lifecycle action="export"` **requires** an `output` argument or validation fails (`mcp/lifecycle.py:148-149`); only one lifecycle job may run at a time — a concurrent submit returns `code="job_already_running"` (`mcp/jobs.py:57-66`).

- [ ] **Step 7: Reconcile the appendix overhead table**

The appendix table and "Combined Impact" row still reference the pre-consolidation 5-tool surface ("5 tool schemas ~650", "Single tool (vs 5 schemas)"). Do not invent new measurements: annotate the table as measured against the superseded 5-tool surface, and reword the status text to cite the shipped 3-tool surface with its regression ceiling ("the consolidated surface is pinned under 1200 tokens by `tests/unit/mcp/test_consolidated_surface.py:79`").

- [ ] **Step 8: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run mkdocs build`

```bash
git add docs/mcp-contract.md
git commit -m "docs(mcp): align the contract with the shipped tool surface, budgets, and error codes"
```

---

## Phase 3 — The rest of the user guide

Five files, one task each, all small. Order matters only for `configuration.md` ↔ `.env.example`, which are fixed together.

### Task 3.1: docs/user/cli-reference.md — analyze is not graph-only; doctor check list

**Files:**
- Modify: `docs/user/cli-reference.md` (`analyze` section ~line 40; `doctor` check list ~lines 97-101)

- [ ] **Step 1: Fix the analyze description**

Current: "Graph only — no semantic index."

Replace with:

> Like `build`, `analyze` stages the call graph **and** the semantic index into one generation and publishes both together (`src/knowcode/service.py:1501-1508`). Differences from `build`: `analyze` requires a directory and adds `--output`/`--coverage`; `build` adds `--incremental`/`--config` and defaults to the current directory.

- [ ] **Step 2: Complete the doctor check list**

The doc lists config, API keys, store schema, index schema/dimensions, disk footprint, unsupported languages, freshness, MCP handshake. Add the missing checks, in code order (`src/knowcode/doctor.py:163-197`): Native dependencies; Optional dependencies; Python runtime; Index generation; **Builder drift** (compares the store manifest's builder fingerprint against the running package; on mismatch the fix is `uv cache clean && uv tool install --force`); Semantic index (fails a dummy-built index when a real embedding provider is configured, and names the missing API key).

- [ ] **Step 3: Note the two smaller gaps**

One sentence each: `telemetry show` also lists telemetry files and warns when raw capture is enabled (`cli.py:603-611`); `mcp-server --store` requires the path to exist when passed explicitly (`click.Path(exists=True)`, `cli.py:970-973`) — only the *implicit* fallback chain (`--store` → `$CLAUDE_PROJECT_DIR` → cwd) is automatic.

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/user/cli-reference.md
git commit -m "docs(cli): analyze builds both planes; complete the doctor check list"
```

### Task 3.2: docs/user/configuration.md + .env.example — entity_source, env-var truth

**Files:**
- Modify: `docs/user/configuration.md` (config table ~lines 44-54; env table ~lines 70-80)
- Modify: `.env.example`

Note: this file carries the Task 0.1 BL-37 commit already; build on top of it.

- [ ] **Step 1: Add `entity_source` to the `config` section table**

New row: `entity_source` — `disk` (default) or `stored` — controls whether `entities.source_code` is read from the working tree with hash verification or served from the store (`src/knowcode/config.py:73,95-99,360-365`).

- [ ] **Step 2: Fix the .env.example claim**

Current (lines ~79-80): "`.env.example` in the repository root documents the same variables."

Replace with: "`.env.example` documents the API keys and base URLs; the `KNOWCODE_*` behavioral variables below are documented only here."

- [ ] **Step 3: Complete the env-var table and the .env.example file**

- Add `KNOWCODE_TESTING` to the doc table, marked test-only (suppresses telemetry writes; `.env.example:39-42`, `telemetry.py:132`).
- Add one commented line to `.env.example`: `# KNOWCODE_TELEMETRY_RAW=1  # opt in to raw local query capture (docs/user/telemetry.md)`. Do **not** add `KNOWCODE_ROUTING_POLICY_ARTIFACT`/`_SHA256` — they are set by tooling, and the doc table already covers them.
- Add to the doc: `prose_embedding_models` is a recognized top-level section with the model-entry shape (`config.py:60,260-274`); the legacy `models:` top-level key is still accepted; validation bounds are `sufficiency_threshold`/`routing_quality_floor`/`hybrid_alpha` ∈ [0,1] and `reranker_top_k_multiplier` ∈ [1,100] (`config.py:299-349`); unknown **top-level** keys warn/raise exactly like unknown `config` keys (`config.py:396-401`).

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run pytest tests/unit -q -k config` (green)

```bash
git add docs/user/configuration.md .env.example
git commit -m "docs(config): document entity_source, prose_embedding_models, and the env-var split"
```

### Task 3.3: docs/user/getting-started.md — scope the freshness claim

**Files:**
- Modify: `docs/user/getting-started.md` (step 5 ~lines 95-96; troubleshooting table ~line 111)

- [ ] **Step 1: Scope the freshness block to MCP**

Current: "Retrieval responses carry a `freshness` block; stale results are flagged, not hidden."

Replace with: "MCP retrieval responses carry a `freshness` block; stale results are flagged, not hidden. CLI output and the REST API report staleness separately (`knowcode doctor`, `GET /api/v1/freshness`)."

- [ ] **Step 2: Fix the troubleshooting row**

Current row: "`is_stale: true` in responses". Change the "where you'll see it" cell to "MCP responses only" (the REST surface never sets it; only `GET /api/v1/freshness` does).

- [ ] **Step 3: Optional micro-cleanup**

Step 1's install command `uv sync --dev --extra all --extra mcp --extra voyageai` is redundant — `all` already includes `mcp` and `voyageai`. Shorten to `uv sync --dev --extra all`.

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/user/getting-started.md
git commit -m "docs(started): freshness is an MCP-response feature; trim redundant extras"
```

### Task 3.4: docs/user/ide-integration.md — batching, minimal fields, lifecycle guardrails

**Files:**
- Modify: `docs/user/ide-integration.md` (Why-MCP step 3 ~lines 18-19; Long builds ~lines 127-133; Tools/lifecycle section)

- [ ] **Step 1: Fix the embedding round-trip paragraph**

Same fact as Task 2.2 Step 4: cross-file batched embedding with bounded concurrency and retry/backoff (`src/knowcode/indexing/embedding_batch.py:12-19`). Keep the job-id design rationale; drop the invented "minutes / tens of minutes" figures.

- [ ] **Step 2: Complete the minimal-response field list**

Where the doc says the agent gets "compact `context_text`, `sufficiency_score`, and token count", append: "plus a `freshness` block, a `reduction_summary` (or `source_included: true` when raw source is included), and `errors` when present."

- [ ] **Step 3: Add the two lifecycle guardrails**

In the bootstrap/lifecycle section: `action="export"` requires `output`; one lifecycle job at a time (`job_already_running` on concurrent submits).

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/user/ide-integration.md
git commit -m "docs(ide): correct batching story, minimal-response fields, lifecycle guardrails"
```

### Task 3.5: docs/user/telemetry.md — the payload-cap fields

**Files:**
- Modify: `docs/user/telemetry.md` (`tool_call` description ~lines 45-47; summary/inspection section; envelope-field note ~line 15)

- [ ] **Step 1: Update the tool_call field list**

Current: "records which MCP tool ran, how many arguments it received, and whether it succeeded."

Replace with: "records which MCP tool ran, **which `action`** it dispatched, `query_id`/`query_chars` when the call carried a query, the argument count, the outcome, **`payload_bytes`** (serialized response size), and `duration_ms` (`src/knowcode/telemetry_policy.py:88-106`, emission in `mcp/server.py:570-593`)."

- [ ] **Step 2: Document the payload_bytes_by_action summary**

Add a short subsection: `get_telemetry_summary` (and therefore `knowcode_inspect action="telemetry"`) returns a `payload_bytes_by_action` block with count and nearest-rank p50/p95/max per action (`telemetry.py:371-393,449`); the CLI `telemetry show` does not print it.

- [ ] **Step 3: Add dropped_field_count**

One sentence: envelope records may also carry `dropped_field_count` when fields were omitted by the allowlist schema (`telemetry_policy.py:36,186-187`).

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/user/telemetry.md
git commit -m "docs(telemetry): record action and payload size on tool_call; document the summary block"
```

---

## Phase 4 — Architecture and internals

Four engineering docs whose mechanism descriptions have drifted from the code. Facts here are subtle — each step cites the source of truth; re-read it while editing.

### Task 4.1: docs/engineering/architecture.md — tool count, ChunkRepository home, Vue row

**Files:**
- Modify: `docs/engineering/architecture.md` (line ~54; lines ~87-88; line ~43)

- [ ] **Step 1: Fix the MCP tool count**

Current: "MCP stdio server (5 tools)". Replace with: "MCP stdio server (3 consolidated tools — `knowcode_retrieve`, `knowcode_lifecycle`, `knowcode_inspect` — plus 5 flat legacy tools behind `mcp-server --legacy-tools`) (`src/knowcode/mcp/tools.py`)."

- [ ] **Step 2: Fix the ChunkRepository location**

Current: "`protocols.py` defines the seams — ... `ChunkRepository` ABC". Replace with: "`protocols.py` defines the embedding/vector/knowledge-store seams; the `ChunkRepository` ABC lives in `src/knowcode/storage/chunk_repository.py:29`."

- [ ] **Step 3: Align the Vue row**

Change the parsers row to the same wording as Task 1.1 Step 2 (custom SFC scanner delegating script blocks to tree-sitter JS/TS).

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/architecture.md
git commit -m "docs(architecture): 3-tool MCP surface, ChunkRepository home, Vue scanner wording"
```

### Task 4.2: docs/engineering/internals/indexing-generations.md — hashing, preflight, provider selection

**Files:**
- Modify: `docs/engineering/internals/indexing-generations.md` (lines ~18-26; artifact list ~lines 36-40)

- [ ] **Step 1: MD5 → SHA-256** — content hashes are SHA-256 (`src/knowcode/indexing/chunker.py:245,369,394`; BL-11).

- [ ] **Step 2: Fix the preflight note** — `unresolved_references` counts legacy `ref::` placeholders **and** `unresolved::` endpoints via `classify_endpoint_id` (`src/knowcode/analysis/preflight.py:21,646-654`; BL-28).

- [ ] **Step 3: Fix provider selection** — selection is config-driven: explicit config → first usable entry of `embedding_models` from `aimodels.yaml` → deterministic dummy fallback (`src/knowcode/llm/embedding.py:461-500`). There is no hardcoded VoyageAI→OpenAI order.

- [ ] **Step 4: Artifact list** — add `preflight_report.json` to the generation-directory listing; add one line noting `chunks.content` is stored zlib-deflated in `chunks.db` (see Task 4.4).

- [ ] **Step 5: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/internals/indexing-generations.md
git commit -m "docs(internals): sha-256 hashing, config-driven embedder selection, preflight hole classes"
```

### Task 4.3: docs/engineering/internals/retrieval-synthesis.md — budget floor, response profiles

**Files:**
- Modify: `docs/engineering/internals/retrieval-synthesis.md` (lines ~30-31; add a response-profiles paragraph)

- [ ] **Step 1: Fix the per-entity budget formula**

Current: "`max(200, min(2000, max_tokens / limit_entities))`". Replace with: "`max(1, min(2000, max_tokens // limit_entities))` — the 200-token floor was removed in BL-29 (`src/knowcode/retrieval/orchestrator.py:185`)."

- [ ] **Step 2: Add a response-profiles paragraph**

> Source inclusion is governed by `src/knowcode/retrieval/response_profiles.py`: `query`/`context` responses are summary-first, and raw source is included only when verbosity ≥ `standard` or the task type is source-hungry (`debug`, `review` — pinned by `tests/unit/retrieval/test_response_profiles.py:44`). `search`/`trace` never carry source; `semantic_search` always does. This supersedes the older `summarize=True` knob described above.

- [ ] **Step 3: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/internals/retrieval-synthesis.md
git commit -m "docs(internals): correct the budget formula and document response profiles"
```

### Task 4.4: docs/engineering/internals/storage-formats.md — schema v4, deflate, honest pointer

**Files:**
- Modify: `docs/engineering/internals/storage-formats.md` (line ~15 and heading ~line 21; lines ~57-59; add a compression section)

- [ ] **Step 1: Schema version** — chunk repository rows and the "SQLite chunk schema (v2)" heading become **v4** (`SqliteChunkRepository.SCHEMA_VERSION = 4`, `sqlite_chunk_repository.py:146`).

- [ ] **Step 2: Document the compression** — new short section: `chunks.content` is stored zlib-deflated (level 6, `0ea0379`); one inflater; the exact-match plane inflates text while scanning; a `schema_meta` table records schema facts; the storage floor is SQLite ≥ 3.43 (`MINIMUM_SQLITE_VERSION`, `sqlite_chunk_repository.py:155`).

- [ ] **Step 3: Fix the stale backlog pointer** — the `see the backlog` passage (a relative link to `../backlog.md`) implies an open disk-cache item; BL-3 was rejected and the Open section is empty. Reword to: "a disk-backed chunk cache was considered and rejected (BL-3); the backlog's Open section is empty."

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run pytest tests/unit/docs -q` (the storage-plan anchor test must stay green)

```bash
git add docs/engineering/internals/storage-formats.md
git commit -m "docs(internals): chunk schema v4, deflate storage, honest backlog pointer"
```

---

## Phase 5 — Engineering reference and process docs

### Task 5.1: docs/engineering/parser-matrix.md — the ADR-11 world

**Files:**
- Modify: `docs/engineering/parser-matrix.md` (header ~lines 1-10; collision paragraph ~lines 50-55; Java limitations; Python row)

- [ ] **Step 1: Fix the framing** — the header calls this "Step 07 snapshot of the hardening blueprint" while also claiming to be "the single source of truth referenced by the release gate". Reword the header: living reference, superseding the archived blueprint (link `../archive/knowcode-parser-concurrency-security-hardening.md` for history).

- [ ] **Step 2: Replace the module-entity collision paragraph** — it describes a collision "resolved in favor of the declaration" that no longer exists. Replace with:

> Since ADR 11 (module-scoped qualified names, BL-9), top-level declarations are module-scoped and no name collision with the file's MODULE entity arises; every tree-sitter parser, including Java and Vue, emits the MODULE entity via `_module_scope` (`src/knowcode/parsers/base.py:122`, `vue_parser.py:128-134`).

- [ ] **Step 3: Update the Java row** — internal IDs are no longer raw `file::name` strings (`java_parser.py:95` uses `_module_scope`); the remaining Java limitations stand: `external::<dotted.name>` endpoints (`java_parser.py:54`) and `ref::` placeholders (`java_parser.py:111,222,235`).

- [ ] **Step 4: Add the receiver-typing stream to the Python row** — one paragraph: reference resolution now binds stated receiver types with repo knowledge, classifies import-bound references, and types module-scope/literal/qualified-return receivers (BL-31→BL-34; commits `8572c33`, `ec330f2`, `d9021c0`, `979f399` for preflight hole classes).

- [ ] **Step 5: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run pytest tests/unit/parsers -q` (green)

```bash
git add docs/engineering/parser-matrix.md
git commit -m "docs(parsers): module-scoped world, receiver-type binding, honest Java limits"
```

### Task 5.2: docs/engineering/api-source.md — cover the new primary surfaces

**Files:**
- Modify: `docs/engineering/api-source.md`

- [ ] **Step 1: Add mkdocstrings entries** for `knowcode.retrieval.response_profiles`, `knowcode.storage.chunk_repository` (the `ChunkRepository` seam), `knowcode.storage.sqlite_chunk_repository`, and `knowcode.routing_policy`, matching the existing entry format. The page claims to "cover the primary extension/maintenance surfaces"; these are now four of them.

- [ ] **Step 2: Verify the API reference renders**

Run: `uv run mkdocs build && grep -c "response_profiles" site/engineering/api-source/index.html` (count > 0)

```bash
git add docs/engineering/api-source.md
git commit -m "docs(api-source): cover response profiles, the chunk repository seam, and routing policy"
```

### Task 5.3: docs/engineering/release.md — watched-edit truth, gitignore truth, build gate

**Files:**
- Modify: `docs/engineering/release.md` (lines ~18, ~32-34, ~43-45, ~82-84)

- [ ] **Step 1: Fix the build gate** — replace the `mkdocs build --strict` gate with the real pair: `uv run mkdocs build` + `uv run python scripts/check_doc_links.py` (same wording as Task 1.1 Step 1).

- [ ] **Step 2: Fix the watched-edit freshness expectation** — current text says doctor warns `store_stale_source_changed` after a watched edit. Reality: the release gate asserts that warning is **absent** after a watch commit (`tests/e2e/test_release_gate_limitations.py:213`, BL-17). Rewrite: "after a watched edit, the Freshness check must **not** report `store_stale_source_changed` — the gate pins this."

- [ ] **Step 3: Fix the known limitation** — current: "a watched edit refreshes retrieval but not the knowledge graph until a rebuild". Reality: the graph is refreshed for the parsed file; only cross-file edges need a rebuild (test renamed `test_a_watched_edit_refreshes_retrieval_and_the_graph`, `test_release_gate_limitations.py:150-223`). Also fix the test module's own docstring drift (lines 19-22) so the gate file matches — this is the one allowed `src/`-adjacent touch, in `tests/`.

- [ ] **Step 4: Fix the "committed knowcode_index/" claim** — `knowcode_index/` is gitignored (`.gitignore:218`); rewrite to "the local `knowcode_index/` is untracked; every checkout builds its own."

- [ ] **Step 5: Verify and commit**

Run: `uv run python scripts/check_doc_links.py && uv run pytest tests/e2e/test_release_gate_limitations.py -q` (green)

```bash
git add docs/engineering/release.md tests/e2e/test_release_gate_limitations.py
git commit -m "docs(release): watched edits refresh graph+retrieval; index is untracked; real docs gate"
```

### Task 5.4: docs/engineering/roadmap-history.md — the split commit

**Files:**
- Modify: `docs/engineering/roadmap-history.md` (lines ~168-169)

- [ ] **Step 1: Fix the commit hash** — the split happened at `3c4043a` ("docs: split the roadmap into a forward plan and a history record"), not `f91fcd2` (which vendored/excluded the gpt-actions-rag clone). Edit the one sentence. Line 13's pre-split use of `f91fcd2` is correct — leave it.

- [ ] **Step 2: Verify and commit**

Run: `git log --oneline | grep 3c4043a` (confirms the hash), `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/roadmap-history.md
git commit -m "docs(history): attribute the roadmap split to the right commit"
```

### Task 5.5: docs/engineering/testing.md — layout caveat (optional, trivial)

**Files:**
- Modify: `docs/engineering/testing.md`

- [ ] **Step 1: Soften the mirror claim** — "layout mirrors `src/knowcode/`" → "layout roughly mirrors `src/knowcode/`; `tests/unit/` also holds top-level config/readiness/telemetry modules."

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/testing.md
git commit -m "docs(testing): soften the layout-mirror claim"
```

---

## Phase 6 — Product, research, ADRs, diagrams, and the stray test scratch file

### Task 6.1: docs/product/personas-use-cases.md — the consolidated tool surface

**Files:**
- Modify: `docs/product/personas-use-cases.md` (UC6 ~`retrieve_context_for_query`; UC7 ~`search_codebase`/`get_entity_context`/`trace_calls`; UC8 ~`assess_codebase_quality`)

- [ ] **Step 1: Rewrite the three use cases around the shipped surface** — UC6: the agent calls `knowcode_retrieve action="query"` (minimal verbosity first, escalate per the ladder); UC7: `knowcode_retrieve action="search"` / `action="trace"`; UC8: `knowcode_inspect action="quality"`. Add one sentence: "The five original flat tools (`retrieve_context_for_query`, `search_codebase`, `get_entity_context`, `trace_calls`, `assess_codebase_quality`) remain available behind `knowcode mcp-server --legacy-tools`." Evidence: `src/knowcode/mcp/tools.py:3-4,49-63,238`; consolidation landed `cb46a46`, canonical contract updated `fc12f6b`.

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/product/personas-use-cases.md
git commit -m "docs(product): use cases follow the consolidated MCP tool surface"
```

### Task 6.2: ADR 0011 amendment — the Vue exception is closed

**Files:**
- Modify: `docs/engineering/adr/adr-0011-module-scoped-qualified-names.md` (append after line ~76)

- [ ] **Step 1: Append a dated amendment** (append-only, per convention):

> **Update (2026-09-22):** The Vue exception above is closed. BL-10 was fixed on 2026-08-31: `VueParser` now emits the file's MODULE entity through `_module_scope` (`src/knowcode/parsers/vue_parser.py:128-134`). Vue is no longer an exception to this ADR.

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/adr/adr-0011-module-scoped-qualified-names.md
git commit -m "docs(adr): record that BL-10 closed ADR 11's Vue exception"
```

### Task 6.3: ADR 0008 update note — storage reality and tool surface

**Files:**
- Modify: `docs/engineering/adr/adr-0008-persistence-format-and-token-economics.md` (extend the existing update note at lines ~7-14; do not rewrite §1/§2 body text)

- [ ] **Step 1: Append a dated update to the note block:**

> **Update (2026-09-22):** Since this ADR was drafted: the knowledge store is SQLite `knowledge.db` published inside each generation (not `knowcode_knowledge.json`; landed `aea573a`, 2026-08-14); the §1.2 artifact set (`chunks.json` / `vectors.index` / `vectors.json`) was superseded by ADR 0009's five-file generation; Option D (SQLite-backed graph store) is implemented in `storage/sqlite_knowledge_store.py`; Phase 2's proposed `SourceResolver` exists as `analysis/live_source_loader.py`; and the MCP surface is three consolidated tools plus five opt-in legacy tools, not the four schemas in §2.1. Status remains Proposed pending a formal disposition of the unshipped phases.

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/adr/adr-0008-persistence-format-and-token-economics.md
git commit -m "docs(adr): dated update note bringing ADR 8 up to the shipped storage reality"
```

### Task 6.4: docs/engineering/adr/index.md — intro covers 10–11

**Files:**
- Modify: `docs/engineering/adr/index.md` (lines ~3-5)

- [ ] **Step 1: Extend the intro sentence** so it reads through ADR 11 — e.g. after the ADR-9 mention, add: "ADR 10 moved ids to root-relative storage; ADR 11 made top-level declarations module-scoped." (The table already lists them; only the prose stops at 9.)

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/engineering/adr/index.md
git commit -m "docs(adr): the index intro now covers ADRs 10 and 11"
```

### Task 6.5: docs/research/knowcode-architecture-synthesis.md — point-in-time banner

**Files:**
- Modify: `docs/research/knowcode-architecture-synthesis.md` (insert after the title, before §1)

- [ ] **Step 1: Insert a status banner:**

> **Status: point-in-time synthesis (2026-08-12).** §1.2 "As-Built" predates the SQLite storage plane — the tree now publishes `knowledge.db`/`chunks.db` inside immutable generations with a LanceDB vector backend (see `docs/engineering/internals/storage-formats.md`, ADR 0009). Several proposals here were subsequently executed by `storage_optimization_2026_v4.md`. Read the baseline section as history, not as a description of the current build.

(Do not rewrite §1 itself — it is a preserved design snapshot; the banner does the honesty work.)

- [ ] **Step 2: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/research/knowcode-architecture-synthesis.md
git commit -m "docs(research): banner marking the 50M-LOC synthesis as a point-in-time record"
```

### Task 6.6: docs/diagrams/README.md — captions match the shipped surface

**Files:**
- Modify: `docs/diagrams/README.md` (rows ~lines 17-19)

- [ ] **Step 1: Fix the seq_mcp caption** — "agent → `retrieve_context_for_query` (minimal first)" becomes "agent → `knowcode_retrieve` (`action="query"`, minimal first)". The diagram PNG/SVG itself is a follow-up (out of markdown scope).

- [ ] **Step 2: Fix the seq_agent_gateway row** — the agent-gateway microservice was removed from this repository in `82fcd14`; reword the row to "Agent Gateway (removed from this repository in `82fcd14`; diagram kept for history)" — or delete the row if the diagram pair should go too. Default: keep + reword, flag the diagram for later removal.

- [ ] **Step 3: Verify and commit**

Run: `uv run python scripts/check_doc_links.py`

```bash
git add docs/diagrams/README.md
git commit -m "docs(diagrams): captions name the shipped tool surface; mark the gateway as removed"
```

### Task 6.7: tests/test_mcp_workflow.md — archive the stale scratch checklist

**Files:**
- Move: `tests/test_mcp_workflow.md` → `docs/archive/test_mcp_workflow.md`

**Why archive, not delete:** it is referenced by nothing (`grep -rn test_mcp_workflow tests src pyproject.toml` → zero hits), it is prose, and both of its expectations are obsolete (local answering is fail-closed and inert per `config.py:137,150,171`; it names the deprecated flat tool). Archiving follows the repo convention ("superseded docs move to `docs/archive/`") and is reversible.

- [ ] **Step 1: Move (controller commits)**

```bash
mkdir -p docs/archive
mv tests/test_mcp_workflow.md docs/archive/test_mcp_workflow.md
```

(Implementers use plain `mv` — the controller runs `git add` on both paths when committing.)

- [ ] **Step 2: Verify** — `uv run python scripts/check_doc_links.py` (archive is excluded from the corpus; also confirm `tests/` still passes: `uv run pytest tests/unit -q`).

### Task 6.8 (optional): docs/index.md — one-line archive note

- [ ] Add one sentence to `docs/index.md`: "Superseded documents live under `docs/archive/`, excluded from this site — never edit them." Then `uv run python scripts/check_doc_links.py` and `git commit -m "docs(index): state the archive convention"`.

---

## Phase 7 — Final acceptance sweep

- [ ] **Step 1: Full gate run**

```bash
uv run python scripts/check_doc_links.py && uv run mkdocs build && uv run pytest -q
```

Expected: all green.

- [ ] **Step 2: Stale-phrase sweep** — each of these strings must no longer appear in the living docs (search `README.md CONTRIBUTING.md docs --include="*.md"` excluding `docs/archive/`):

  - `mkdocs build --strict`
  - `Graph only — no semantic index`
  - `one embedding round-trip per file`
  - `max(200, min(2000`
  - `chunk schema` (as "v2")
  - `5 tools` (in architecture.md)
  - `reduced from \`6000\` to \`4000\` across`
  - `retrieve_context_for_query` (outside a "legacy tools" mention)
  - `MD5` (in indexing-generations.md)
  - `f91fcd2` (in roadmap-history.md line ~168)
  - `](CHANGELOG.md)` (in README.md — the untracked-changelog link is gone)

- [ ] **Step 3: CHANGELOG** — ensure the Unreleased section carries a `docs:` line summarizing this refresh.

- [ ] **Step 4: Update `docs/engineering/backlog.md`?** No — the Open section is empty by design and this work is tracked by this plan. If any item was skipped, file it as BL-38+ per the existing row format instead of silently dropping it.

- [ ] **Step 5: Final commit** (changelog line, if any) and a summary of phases completed in the PR description.

---

## Self-Review (done at plan time)

- **Coverage:** every file flagged by the four audits has a task, or an explicit in-constraint reason for exclusion (frozen ADRs 1–7/9/10, `docs/archive/**`, test fixtures, gpt-actions-rag, `.agent/rules/*` — audited current, no action; `docs/product/overview.md` and `business-logic.md` — audited current; `docs/roadmap.md` — audited consistent; `.agents/workflows/fix-mypy-errors.md` — current with a stylistic caveat, no factual drift).
- **Placeholders:** all edits specify exact replacement text or a fully-specified content requirement with source citations; no "TBD"/"add appropriate content" steps.
- **Cross-task consistency:** the Vue wording is defined once (Task 1.1) and reused (Task 4.1); the build-gate wording is defined once (Task 1.1) and reused (Tasks 5.3); the changelog decision (Task 1.4) is referenced by Task 1.2 and closed in Phase 7.
