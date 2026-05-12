# KnowCode Operationalization Priorities

**Date:** 2026-05-12
**Status:** Plan (not yet executed)
**Owner:** Solo
**Scope:** Make KnowCode trustworthy for daily use as the repository-context provider for AI coding agents.

## Context

KnowCode is intended to be invoked by the following agent CLIs/IDEs as the source of repository-level context:

- Claude Code
- Codex
- Hermes Agent CLI
- Gemini CLI
- Antigravity IDE

Target repository: whichever repo KnowCode is run from (solo, one repo at a time). Team features (RBAC, sharing) are out of scope.

The questions this plan is built to answer:

1. Is KnowCode ready for production use?
2. Is the MCP interface optimized to minimize token consumption (schema load in the consumer's context window)?
3. What gaps am I not yet aware of?

## Evidence baseline (already done — do not re-do)

- **Phase 4.5 architectural hardening AD-1 through AD-7** is complete (see `KnowCode.md` Phase 4.5 checklist; commits `983e6b4`, `8f9a8cd`).
- Of the 5 MCP token-reduction strategies in `docs/MCP_TOKEN_OVERHEAD_REDUCTION.md`:
  - Strategy 3 (lower defaults): partially done — `get_entity_context` default is now `max_tokens=2000`.
  - Strategy 4 (compact JSON): done — `json.dumps(separators=(',', ':'))` at `src/knowcode/mcp/server.py:344`.
  - Strategy 2 (stripped response): partially done — `verbosity="minimal"` default on `retrieve_context_for_query`.
  - Strategy 1 (consolidate to one tool): **NOT done** — 4 tools still injected per turn.
  - Strategy 5 (summaries over full source): **NOT done**.
- Item **#22 "Layer Contract Tests" is the only unchecked box** in Phase 4.5 of the roadmap.
- Per-agent rule files exist in the repo (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.agent/rules/`, previously `.kilocode/rules/`) but are not maintained from one canonical source and are not installed by a single command.

## Priorities

### P1 — Production-readiness verification

**Problem it addresses:** "I don't know if KnowCode is ready for production."

**Deliverables:**

1. Implement the last open Phase 4.5 item (#22 "Layer Contract Tests"):
   - Parser → `ParseResult` contract tests.
   - Knowledge store save/load roundtrip with `schema_version` assertions.
   - Retrieval golden-query tests (fixed query → fixed top-K entity IDs, regression-guarded).
   - CLI smoke tests via `click.testing.CliRunner`.
   - API endpoint contract tests, conditional on the `server` extra.
2. Add a `knowcode doctor` CLI subcommand. Target wall-clock: under 10s. Checks:
   - Knowledge store present and `schema_version` recognized.
   - Index present, dimension matches configured embedding model.
   - Required API keys present for the providers enabled in `aimodels.yaml`.
   - MCP handshake works: spawn the MCP server, `list_tools`, call one tool, parse the response.
   - Configuration loads cleanly under `strict_config=True`.
   - Disk footprint under a configurable cap (warn, do not fail).
3. Treat `doctor` green as a precondition for landing P2–P5.

**Why first:** Without it, every subsequent change is built on uncertainty. Directly addresses the production-readiness question.

### P2 — Finish the MCP token diet

**Problem it addresses:** "I don't know whether the MCP interface is optimized for token consumption."

**Deliverables:**

1. **Strategy 1 — consolidate to one tool.** Merge `search_codebase`, `get_entity_context`, `trace_calls`, `retrieve_context_for_query` into a single `knowcode` tool with an `action` enum (`search` | `context` | `trace` | `query`). Keep the four-tool surface available behind a deprecation flag for one release. Expected saving: roughly 400 tokens per agent turn, across every consumer CLI.
2. **Strategy 5 — summary-first responses.** Default `context_text` to signature + docstring + caller/callee IDs. Return full source only when `task_type in {debug, review}` or when the caller explicitly sets `include_source=true`. Expected reduction: `context_text` from ~3000 → ~500 tokens for exploratory queries.
3. Add a regression-guard fixture asserting that the byte size of each `action`'s response stays under fixed caps. Fail the test on creep.
4. Update `docs/MCP_TOKEN_OVERHEAD_REDUCTION.md` to mark Strategies 1 and 5 as shipped with measured before/after numbers.

**Why second:** These are the last two unshipped items in the token-overhead doc you already authored. Together they close the gap to the ≈88% reduction target.

### P3 — Unified five-consumer onboarding

**Problem it addresses:** Five agent CLIs, five different config locations, no single setup path, no end-to-end verification.

**Deliverables:**

1. `knowcode install-agent <claude-code|codex|hermes|gemini-cli|antigravity>` writes the correct MCP-server config (or equivalent) into the right location per agent, idempotently. Detect existing config and either skip or merge.
2. One canonical rules snippet at `.knowcode/agent-rules.md` describing when to call KnowCode versus `grep`/`Read`, recommended `action` per intent, and token-budget guidance. `install-agent` appends or `@include`s this snippet from each agent's native rules file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, etc.).
3. `knowcode doctor --agent <name>` verifies the named agent can actually invoke a tool end-to-end, not just that the config file exists on disk.
4. Document the exact config syntax and config-file path for each of the five consumers; flag any consumer whose syntax we have not yet verified (currently: Hermes Agent CLI).

**Why third:** Without it, "production" requires five manual setups per repo with divergent quirks. With it, one command per consumer.

### P4 — Freshness and lifecycle automation

**Problem it addresses:** Five agents querying a knowledge store that silently goes stale during normal dev.

**Deliverables:**

1. `.knowcode/freshness.json` tracks `store_commit_sha`, `index_commit_sha`, last successful `analyze`/`index` timestamps.
2. MCP tool responses include a `freshness` field with `commits_behind_head` and `last_indexed_at`, so the consuming agent can flag potentially stale answers in its response to the user.
3. `knowcode install-hooks` installs a git `post-commit` hook that enqueues an incremental `analyze` + `index` run, backgrounded.
4. `knowcode server --watch --daemon` plus launchd (macOS) and systemd (Linux) unit-file templates for solo always-on mode.

**Why fourth:** Once five agents start hitting the store, stale data poisons answers without any visible failure mode. Freshness must be in the response payload, not a thing the developer remembers to rebuild.

### P5 — Usage observability loop

**Problem it addresses:** "What else don't I know?" Closing the loop on whether KnowCode is actually being used and is actually earning its keep.

**Deliverables:**

1. Append every MCP tool call to `.knowcode/telemetry.jsonl`, with: action, latency, response token estimate, `sufficiency_score`, `commits_behind_head` at call time, and the agent identifier when discoverable from the request context.
2. `knowcode stats --usage [--since 7d]` summarizes: calls per day, mean sufficiency score, local-answer rate (calls with score >= threshold), stale-response count, per-agent breakdown.
3. Weekly self-report surface (CLI, not a dashboard) so usage is visible at a glance.

**Why last:** Nothing to observe until P1–P4 ship. Once they have, this is what turns "in production" into "trusted in production."

## Sequencing

- **P1 and P2 can run in parallel** — different subsystems, no shared files.
- **P3 depends on P2.** The rules snippet, install commands, and docs in P3 should refer to the consolidated single-tool surface, not the legacy four-tool interface, to avoid rework.
- **P4 and P5 can run in parallel** after P3.

## Open verification items

Before starting execution, I have not yet verified the following. Each could affect scope:

1. Actual current test coverage percentage and which subsystems are weakest.
2. Whether any partial implementation of `knowcode doctor` already exists.
3. The exact MCP configuration syntax and file path for Hermes Agent CLI.
4. Whether the `apps/agent-gateway/` OpenAPI-to-tool path should be promoted as an alternative for any of the five consumers, or kept as a separate power-user option.

## Non-goals (explicitly out of scope here)

- Team features: RBAC, shared remote stores, audit logging (Phase 6).
- Phase 5 deep analysis: data flow, intent extraction, confidence scoring.
- Phase 4 multi-level documentation synthesis.
- SQLite-backed storage for large monorepos (AD-8).
