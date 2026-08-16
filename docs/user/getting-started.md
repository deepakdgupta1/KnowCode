# Getting Started

A first run of KnowCode, from install to answering questions about your
codebase. Full flag-level detail for every command lives in the
[CLI reference](cli-reference.md).

## 1. Install

From a repository checkout (development):

```bash
uv venv
source .venv/bin/activate          # On Windows: .venv\Scripts\activate
uv sync --dev --extra all --extra mcp --extra voyageai
```

From an installed package (usage): a plain `pip install knowcode` gives you
the lightweight core; then run `knowcode install` to add the ideal runtime
feature set (search, llm, server, watch, mcp, voyageai), or install just the
extras you need — see [Configuration](configuration.md#optional-extras).

If your machine's default Python is 3.13+, pin 3.12 when running via `uvx`
(tree-sitter-languages publishes wheels through cp312):

```bash
uvx --python 3.12 --from "knowcode[all]" knowcode doctor
```

## 2. Set API keys (optional)

Retrieval runs 100% locally. Keys are only needed for optional features —
embeddings and reranking (semantic search quality) and the LLM behind
`knowcode ask`. Without an embedding key, search still works but is
lexical-only.

```bash
export VOYAGE_API_KEY_1="..."   # embeddings + reranking (semantic search)
export GLM_API_KEY="..."        # LLM for `knowcode ask` (default provider)
```

See [Configuration](configuration.md#environment-variables) for all variables
and how model entries pick the variable they read.

## 3. Verify readiness

```bash
knowcode doctor --store . --mcp
```

`doctor` checks strict config loading, required API keys, store and index
schemas, embedding dimensions, artifact disk footprint (default 500 MB
threshold), unsupported-language warnings, freshness, and — with `--mcp` — a
live MCP stdio handshake. Fix what it reports before building.

## 4. Build the knowledge base

```bash
knowcode build .
```

One atomic step: scan → parse → graph → chunks → embeddings → publish. It
prints entity/relationship counts and a **preflight report card**. A failed
build leaves the previous generation in place — you are never left with a
half-built index.

The report card grades your codebase on 10 dimensions (documentation
density, naming, parse success rate, …) with A–F grades and recommendations.
It predicts how well KnowCode will serve you: well-documented, clearly named
code retrieves better. `A/B` grades mean you are in good shape; `D/F` grades
come with concrete recommendations (e.g., add docstrings — the strongest
single signal for retrieval). You can re-run it standalone:

```bash
knowcode preflight .
```

## 5. Ask questions

```bash
# Lexical graph queries
knowcode query search "Parser"
knowcode query callers "GraphBuilder.build_from_directory"

# Natural-language semantic search
knowcode semantic-search "Where is the graph built?"

# Token-budgeted context for one entity
knowcode context "GraphBuilder.build_from_directory"

# LLM Q&A over the retrieved context
knowcode ask "How does the graph builder work?"
```

Keep artifacts fresh: after significant code changes, re-run `knowcode build
.` (or `knowcode server --watch` to do it automatically). Retrieval
responses carry a `freshness` block; stale results are flagged, not hidden.

## 6. Connect your IDE agent

The biggest token savings come from wiring your AI coding agent (Claude
Desktop, VS Code, Antigravity, …) to KnowCode over MCP so it retrieves
compact context locally instead of reading whole files. See
[IDE Integration](ide-integration.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Install knowcode[server] to use 'knowcode server'.` | Install the named extra (`pip install "knowcode[server]"`) or run `knowcode install` |
| Doctor reports missing keys | Export the env vars named by your `aimodels.yaml` model entries |
| `is_stale: true` in responses | Re-run `knowcode build .`; if running the server, `POST /api/v1/reload` |
| Semantic search falls back to lexical | Rebuild the index (`knowcode index .`) and confirm embedding keys are set |
| Retrieval misses obvious code | Check the preflight report card; heavily undocumented code retrieves worse; excluded languages are listed in the [language matrix](cli-reference.md#supported-language-matrix) |
| Disk usage warnings | Artifacts grow with repo size — see `knowcode doctor --max-disk-mb`; old index generations are retired automatically |

For what KnowCode records locally while you work (nothing leaves your
machine), see [Telemetry & Privacy](telemetry.md).
