# KnowCode

Know a codebase using KnowCode. Ask questions and get responses in natural
language about a codebase to learn more about it. Provide accurate, relevant
context to your AI coding agent and make its token usage limits last 10x
longer.

KnowCode analyzes a codebase and builds a semantic graph of entities
(functions, classes, modules) and their relationships (calls, imports,
dependencies), then serves **token-efficient context bundles** to AI agents
through a CLI, a local REST server, and an MCP server. Retrieval is 100%
local and deterministic; LLMs are only needed for optional features
(embeddings, reranking, Q&A).

## Installation

```bash
# Create and activate virtual environment
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install KnowCode for development (batteries included)
uv sync --dev --extra all --extra mcp --extra voyageai

# Or, from an installed KnowCode CLI, install the ideal runtime setup
knowcode install

# If using uvx from a machine whose default Python is 3.13+,
# pin Python 3.12 because tree-sitter-languages publishes wheels through cp312.
uvx --python 3.12 --from "/path/to/KnowCode[all,mcp,voyageai]" knowcode doctor

# Set API keys (only needed for the features you use; see aimodels.yaml)
export VOYAGE_API_KEY_1="..."   # embeddings + reranking (semantic search)
export OPENAI_API_KEY="..."     # embeddings (alternative to VoyageAI)
export GOOGLE_API_KEY="..."     # LLM (Gemini) for `knowcode ask`
```

KnowCode ships with a lightweight core plus feature extras — `server`,
`search`, `llm`, `watch`, `mcp`, `voyageai`, or `all`. Missing extras fail
fast with actionable hints, e.g. `Install knowcode[server] to use 'knowcode
server'.`

## Quick Start

```bash
# 1. Build the knowledge base and semantic index for the current directory
knowcode build .

# 2. Verify codebase readiness and MCP server handshake
knowcode doctor --store . --mcp

# 3. Query the knowledge store
knowcode query search "MyClass"
knowcode query callers "my_function"

# 4. Generate context for an entity
knowcode context "MyClass.important_method"

# 5. Ask questions using the LLM agent
knowcode ask "How does the graph builder work?"

# 6. Start the intelligence server with watch mode
knowcode server --port 8080 --watch
```

## Documentation

All 16 commands with flags and defaults live in the
[CLI reference](docs/user/cli-reference.md).

| Audience | Start here |
|---|---|
| Users | [CLI reference](docs/user/cli-reference.md) · [IDE agent setup](docs/MCP_SETUP.md) · [REST API](docs/diagrams/README.md#fastapi-rest-api-8000-uvicorn) |
| Product managers | [Use-case narratives](docs/architecture/reference_architecture.md) (dedicated product docs in progress) |
| Engineers | [Reference architecture](docs/architecture/reference_architecture.md) · [Hardening contracts (ADRs)](docs/architecture/hardening-contracts.md) · [Parser construct matrix](docs/architecture/parser-construct-matrix.md) · [Roadmap](docs/roadmap.md) |

Supported languages: Python, JavaScript/JSX, TypeScript/TSX, Java, Rust,
Vue, Markdown, and YAML. Other extensions (`.go`, `.cpp`, `.swift`, …) are
ignored during analysis — see the
[language matrix](docs/user/cli-reference.md#supported-language-matrix) for
details and per-parser coverage.

The canonical retrieval policy for AI agents is the
[MCP contract](docs/mcp-contract.md); keep agent rules pointed there instead
of hard-coding separate thresholds or token budgets in each client.

## Architecture

KnowCode follows a layered pipeline: **Scanner** (file discovery with
gitignore support) → **Parsers** (Python AST; tree-sitter for JS/TS/Java/
Rust/Vue; custom Markdown/YAML) → **Graph Builder** (semantic graph) →
**Knowledge Store** (in-memory + JSON/SQLite persistence) → **Indexer**
(embeddings + hybrid BM25/vector retrieval) → **Context Synthesizer**
(token-budgeted bundles with priority ranking) → **CLI / REST / MCP
surfaces**.

See the [reference architecture](docs/architecture/reference_architecture.md)
for the complete picture, including the atomic index-generation system and
watch mode.

## Observability

KnowCode logs local, non-blocking telemetry to trace query performance,
routing decisions, and MCP tool-call patterns. Telemetry never leaves the
machine and never contains questions or code — see
[docs/observability.md](docs/observability.md) for the schema, privacy
trade-offs, and threshold tuning.

## Development

```bash
pytest              # tests (conftest sets KNOWCODE_TESTING=1)
mypy src/           # strict type checking
ruff check src/     # lint
ruff format src/    # format
mkdocs build --strict  # docs site must build warning-free
```

The forward plan and release gates live in the [roadmap](docs/roadmap.md);
release history in the [changelog](CHANGELOG.md).

## License

[MIT](LICENSE)
