# Product Overview

What KnowCode is, the problem it solves, what it deliberately is not, and
where it sits relative to alternatives. Personas and use-cases are in
[Personas & Use-Cases](personas-use-cases.md); the reasoning behind every
user-visible behavior is in [Business Logic](business-logic.md).

## The problem

AI coding agents are context-limited. To answer a question or make a change,
an agent stuffs files into its prompt until it runs out of token budget —
expensive, slow, and imprecise. The information the agent needs (which
function calls which, where a type is defined, what a change touches) is
*structural*, and structure can be computed once, locally, and reused by
every query.

## The product

KnowCode analyzes a codebase into a **semantic knowledge graph** — entities
(functions, classes, modules, documents, config keys) and relationships
(calls, imports, containment, inheritance) — plus a **hybrid search index**
(BM25 keyword + vector embeddings) over code chunks. On top of that it
serves **token-budgeted context bundles**: for any question or entity, the
smallest slice of the codebase that is likely sufficient, with an explicit
`sufficiency_score` saying how likely.

Three delivery surfaces, one engine:

- **CLI** — 16 commands for humans (`build`, `query`, `ask`, `doctor`, …)
- **REST server** — local FastAPI endpoints for locally hosted agents
- **MCP server** — stdio protocol for IDE agents (Claude Desktop, VS Code,
  Antigravity, …)

## Value proposition

| Claim | Mechanism |
|---|---|
| ~10x longer token budgets for agents | Agents retrieve compact, ranked context bundles instead of whole files; `verbosity=minimal` responses omit raw source when summarizing suffices |
| Local-first, deterministic | Retrieval, graph queries, and call-tracing run entirely on-machine with no LLM in the loop; the same query returns the same result |
| Correctness over cost | Local answering (no LLM at all) is **disabled by default** and only unlocks behind a machine-verified quality gate (see [Business Logic](business-logic.md#local-vs-llm-routing-fail-closed-by-design)) |
| Trust signals built in | Every response carries freshness state; a 10-dimension preflight report card predicts how well KnowCode will work on a given repo |
| Privacy by construction | Telemetry is local, aggregate-only, and structurally cannot contain questions or code |

## Positioning

- **Not hosted, not multi-tenant.** Single user, single repository, local
  disk. There is no server to deploy and no data leaves the machine except
  optional embedding/LLM API calls you configure.
- **Not a code-search replacement** for humans (grep/IDE search already do
  that); it is the context supplier *for AI agents* and the graph explorer
  for structural questions.
- **Adjacent to RAG tooling** but code-specialized: chunks are
  entity-aligned (signature + docstring + source), retrieval is fused with
  graph relationships, and outputs are token-budgeted rather than
  document-ranked.

## Non-goals (explicit)

From the [roadmap](../roadmap.md): hosted/multi-tenant service; the HTTP
gateway as a primary path; auto-rewriting agent configurations;
unverified consumer compatibility; and any cost optimization that bypasses
quality gates. Language coverage is fixed at Python, JS/TS, Java, Rust,
Vue, Markdown, reStructuredText, YAML — other extensions are ignored.

## Business model constraints worth knowing

Optional features consume external APIs: embeddings + reranking (VoyageAI)
and Q&A (`ask`) consume LLM tokens. Retrieval-only operation needs no keys
at all — the product degrades to lexical search rather than failing.
Rate-limiter configuration keeps `ask` inside provider free tiers.
