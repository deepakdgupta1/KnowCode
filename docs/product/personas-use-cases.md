# Personas & Use-Cases

Who uses KnowCode, what they are trying to accomplish, and how the product
delivers it. Every use-case names the surface (CLI / REST / MCP) and the
commands or tools involved; full command detail is the
[CLI reference](../user/cli-reference.md).

## Personas

| # | Persona | Context | Primary surface |
|---|---|---|---|
| P1 | **Developer explorer** — engineer (often new to a codebase) asking structural questions | "What calls this? What happens if I delete it?" | CLI |
| P2 | **AI coding agent** — the *primary* persona: an IDE agent burning external LLM tokens on file-reading | Claude Desktop, VS Code, Antigravity, Cursor | MCP |
| P3 | **Locally hosted agent / service** — a custom automation or agent framework on the same machine | Scripted pipelines, function-calling agents | REST |
| P4 | **Maintainer / evaluator** — the person deciding whether to trust KnowCode on a given repository | Setup verification, quality assessment, tuning | CLI (`doctor`, `preflight`, `telemetry`) |

## Use-cases by persona

### P1 — Developer explorer

**UC1: Bootstrap understanding of a repository.**
`knowcode build .` → `knowcode stats`. The preflight report card that
prints during build tells the developer how *knowable* this codebase is
(documentation density, naming, unresolved references) before they invest
time. Success: entity/relationship counts and a quality grade in under a
minute of work.

**UC2: Ask structural questions in plain English.**
`knowcode ask "How does the graph builder work?"` — the query is
classified (explain/debug/extend/review/locate/general), minimal context is
retrieved, and the LLM answers with citations back to source. Without an
LLM key, `knowcode semantic-search "…"` and `knowcode query callers "…"`
still answer structurally.

**UC3: Trace impact before changing code.**
`knowcode query callers/deps <entity>`; via REST,
`GET /api/v1/trace_calls/{id}?depth=3` and `GET /api/v1/impact/{id}`
return multi-hop dependents and a 0–1 risk score (see
[Business Logic](business-logic.md#impact-risk-scoring)).

**UC4: Generate starter documentation.**
`knowcode export` produces deterministic, no-LLM multi-level Markdown
(index, architecture overview, per-module pages) plus a freshness manifest.
Success: reviewable docs immediately; LLM narratives can layer on later.

**UC5: Understand change history.**
`knowcode build . --temporal` then `knowcode history [entity]` surfaces
git authorship and per-entity change history.

### P2 — AI coding agent (primary)

**UC6: Retrieve context without reading files.**
The agent calls MCP tool `retrieve_context_for_query` with
`verbosity="minimal"`; KnowCode returns compact `context_text`, a
`sufficiency_score`, and token counts. Escalation (more entities, then more
detail) happens only when the score is below threshold. The canonical
policy is the [MCP contract](../mcp-contract.md).

**UC7: Look up known symbols precisely.**
MCP tools `search_codebase`, `get_entity_context`, `trace_calls` for
follow-up once retrieval identifies the entity of interest.

**UC8: Judge a codebase before working in it.**
MCP tool `assess_codebase_quality` returns the preflight report card, so an
agent (or its user) can decide how much to trust local context on an
unfamiliar repository.

### P3 — Locally hosted agent / service

**UC9: Serve codebase intelligence over HTTP.**
`knowcode server --watch` exposes 12 endpoints (search, context bundles,
semantic query, call tracing, impact, freshness) with two rate-limit tiers.
Non-MCP frameworks wrap `/openapi.json` as function-calling tools — see
[IDE & Agent Integration](../user/ide-integration.md#non-mcp-agent-frameworks-openapi-function-calling).

**UC10: Stay fresh while code changes.**
Watch mode re-indexes modified/created files and invalidates chunks of
deleted/moved ones; `GET /api/v1/freshness` and per-response freshness
blocks flag staleness explicitly.

### P4 — Maintainer / evaluator

**UC11: Verify readiness.**
`knowcode doctor --store . --mcp` checks config strictness, API keys,
schema versions, embedding dimensions, disk footprint, and performs a live
MCP handshake.

**UC12: Predict retrieval quality on a repository.**
`knowcode preflight <dir>` grades 10 dimensions (weighted) with
recommendations — documentation density and parse success rate dominate.
This is the honest-answer use-case: some codebases are poor fits, and
preflight says so before anyone is disappointed.

**UC13: Tune the cost/accuracy dial with evidence.**
`knowcode telemetry show` reports local routing rate, average sufficiency,
and misses; the [tuning loop](../user/telemetry.md#threshold-tuning) says
when to move `sufficiency_threshold`.

## Use-case → capability map

| Capability (shared engine) | Serves |
|---|---|
| Knowledge graph + graph queries | UC1, UC3, UC5, UC7 |
| Hybrid retrieval + reranking | UC2, UC6, UC9 |
| Context synthesis + sufficiency scoring | UC2, UC6 |
| Preflight assessment | UC1, UC8, UC12 |
| Generations / incremental indexing / watch | UC10, all |
| Telemetry | UC13, product analytics (local only) |
| LLM agent (`ask`) with escalation ladder | UC2 |
