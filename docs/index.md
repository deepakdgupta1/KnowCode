# KnowCode

Transform your codebase into an effective knowledge base that provides accurate, relevant context for AI coding assistants—using minimal tokens.

[![CI/CD Pipeline](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml)


## Overview

KnowCode analyzes your codebase and builds a semantic graph of entities (functions, classes, modules) and their relationships (calls, imports, dependencies). This structured knowledge enables:

- **Accurate context synthesis** for AI assistants
- **Token-efficient** context generation (only what's needed)
- **Local-first** querying without LLM dependency
- **Traceability** back to source code

## Installation

```bash
# Create and activate virtual environment
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install KnowCode for development (batteries included)
uv sync --dev --extra all --extra mcp --extra voyageai

# Set API keys (only needed for the features you use; see aimodels.yaml)
export VOYAGE_API_KEY_1="..."   # embeddings + reranking (semantic search)
export OPENAI_API_KEY="..."     # embeddings (alternative to VoyageAI)
export GOOGLE_API_KEY_1="..."   # LLM (Gemini) for `knowcode ask`
```

## Quick Start

The recommended workflow for any new repository is to build the knowledge base and semantic index in one step, then run the doctor to verify readiness:

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

## Commands

### `build`
Build the knowledge base and semantic index for a directory in one step. This is the recommended starting point for any new repository.

```bash
knowcode build <directory> [--ignore <pattern>] [--config <path>]
```

**Example:**
```bash
knowcode build .  # builds for the current directory
```

Internally `build` runs `analyze` followed by `index`. Use `analyze` and `index` separately only if you need fine-grained control over individual steps.

### `analyze`
Scan and parse a directory to build the knowledge store (without building the semantic index).

```bash
knowcode analyze <directory> [--output <path>] [--ignore <pattern>]
```

**Example:**
```bash
knowcode analyze src/ --ignore "tests/*" --ignore "*.pyc"
```

### `query`
Query the knowledge store for relationships.

```bash
knowcode query <type> <target> [--store <path>] [--json]
```

**Query types:**
- `search <pattern>` - Search entities by name
- `callers <entity>` - Find what calls this entity
- `callees <entity>` - Find what this entity calls
- `deps <entity>` - Get all dependencies

**Example:**
```bash
knowcode query search "Parser"
knowcode query callers "GraphBuilder.build_from_directory"
knowcode query deps "PythonParser" --json
```

### `context`
Generate a context bundle for an entity (ready for AI consumption).

```bash
knowcode context <entity> [--store <path>] [--max-tokens <n>]
```

**Example:**
```bash
knowcode context "GraphBuilder.build_from_directory" --max-tokens 4000
```

### `export`
Export the knowledge store as multi-level Markdown documentation. The export
includes an index, architecture overview, per-module pages, and a manifest with
entity content hashes for freshness checks.

```bash
knowcode export [--store <path>] [--output <dir>]
```

**Example:**
```bash
knowcode export -o docs/
```

### `stats`
Show statistics about the knowledge store.

```bash
knowcode stats [--store <path>]
```

### `doctor`
Check whether the local KnowCode setup is ready for daily use.

```bash
knowcode doctor [--store <path>] [--index <path>] [--config <path>] [--mcp] [--json]
```

Checks include strict config loading, required model API keys, knowledge store
schema, semantic index schema/embedding dimensions, artifact disk footprint,
and optionally an MCP stdio handshake.

### `index`
Build a semantic search index for your codebase.

```bash
knowcode index <directory> [--output <path>] [--config <path>]
```

### `semantic-search`
Perform a natural language search against the semantic index.

```bash
knowcode semantic-search <query> [--index <path>] [--store <path>] [--config <path>] [--limit <n>]
```

**Example:**
```bash
knowcode semantic-search "Where is the graph built?"
```

### `server`
Start the FastAPI intelligence server. This is the preferred way for locally hosted AI agents (IDEs) to interact with KnowCode.

```bash
knowcode server [--host <host>] [--port <port>] [--store <path>] [--watch]
```

**Example:**
```bash
knowcode server --port 8080
```

Once running, you can access endpoints like:
- `GET /api/v1/context?target=MyClass&task_type=debug`
- `GET /api/v1/search?q=parser` `(lexical search)`
- `POST /api/v1/context/query` `(semantic search)`
- `GET /api/v1/trace_calls/{entity_id}?direction=callers&depth=3` `(multi-hop call graph)`
- `GET /api/v1/impact/{entity_id}` `(deletion impact analysis)`
- `GET /api/v1/freshness` `(artifact freshness and staleness report)`
- `POST /api/v1/reload` (to refresh data after a new `analyze` run)

### `history`
Show git history for the codebase or specific entities. Requires analysis with `--temporal`.

```bash
knowcode history [target] [--limit <n>]
```

**Example:**
```bash
# Show recent project history
knowcode history --limit 5

# Show history for a specific class
knowcode history "KnowledgeStore"
```

### `ask`
Ask questions about the codebase using an LLM agent. Requires an API key for at least one configured model in `aimodels.yaml`.

```bash
knowcode ask <question> [--config <path>]
```

**Configuration:**
KnowCode looks for a configuration file in the following order:
1. `--config` argument
2. `aimodels.yaml` in current directory
3. `~/.aimodels.yaml`

**Example `aimodels.yaml`:**
```yaml
natural_language_models:
  - name: gemini-2.5-flash
    provider: google
    api_key_env: GOOGLE_API_KEY_1
```

**Example:**
```bash
knowcode ask "How does the graph builder work?"
```

### `mcp-server`
Start an MCP (Model Context Protocol) server for IDE agent integration.

```bash
knowcode mcp-server [--store <path>] [--config <path>]
```

For token-efficient IDE agent usage, follow the canonical
[MCP retrieval contract](mcp-contract.md).

**Tools Exposed:**
- `search_codebase` - Search for code entities by name
- `get_entity_context` - Get detailed context for an entity
- `trace_calls` - Trace call graph (callers/callees) with depth
- `retrieve_context_for_query` - Unified query→retrieval→context bundle

## Supported Language Matrix

KnowCode scans, parses, and indexes codebases to construct semantic graphs. Below is the support status for various file extensions and programming languages:

| Extension | Language | Parser Mechanism | Discovery Status | Notes |
|---|---|---|---|---|
| `.py` | Python | Python AST | Fully Discovered & Parsed | Full semantic parsing (Python 3.10 - 3.12). |
| `.js`, `.jsx` | JavaScript | Tree-sitter | Fully Discovered & Parsed | Extracts classes, functions, imports, JSX tags. |
| `.ts`, `.tsx` | TypeScript | Tree-sitter | Fully Discovered & Parsed | Extracts classes, functions, imports, TSX tags. |
| `.java` | Java | Tree-sitter | Fully Discovered & Parsed | Extracts classes, methods, imports, inheritance. |
| `.rs` | Rust | Tree-sitter | Fully Discovered & Parsed | Extracts structs, enums, functions, impl blocks. |
| `.vue` | Vue | Tree-sitter | Fully Discovered & Parsed | Extracts Vue Single-File Component scripts. |
| `.md` | Markdown | Custom Markdown parser | Fully Discovered & Parsed | Document structure with heading hierarchy. |
| `.yaml`, `.yml` | YAML | Custom YAML parser | Fully Discovered & Parsed | Configuration keys with nested structure. |

### Unsupported Extensions
Any file extensions not explicitly listed in the table above (e.g. `.go`, `.cpp`, `.h`, `.swift`, `.rb`, `.php`, `.css`, `.html`) are currently **ignored** during index/analyze operations.

## Architecture

KnowCode follows a layered architecture:

1. **Scanner** - Discovers files with gitignore support
2. **Parsers** - Language-specific parsing (Python AST, Tree-sitter for others)
3. **Graph Builder** - Constructs semantic graph with entities and relationships
4. **Knowledge Store** - In-memory graph with JSON persistence
5. **Indexer** - Vector embedding and hybrid retrieval engine (FAISS + BM25)
6. **Context Synthesizer** - Generates token-efficient context bundles with priority ranking
7. **CLI** - User interface for all operations

See [reference_architecture.md](file:///Users/deepg/Desktop/KnowCode/docs/architecture/reference_architecture.md) for the complete reference architecture.

## Example Output

**Stats:**
```
Total Entities: 98
  class: 15
  function: 6
  method: 66
  module: 11

Total Relationships: 616
  calls: 478
  contains: 87
  imports: 47
  inherits: 4
```

**Context Bundle:**
```markdown
# Method: `GraphBuilder.build_from_directory`

**File**: `/path/to/graph_builder.py`
**Lines**: 24-45

## Description
Build graph by scanning and parsing a directory.

## Signature
def build_from_directory(self, root_dir: str | Path, ...) -> 'GraphBuilder'

## Source Code
[full source code]

## Called By
- `main`
- `analyze_command`

## Calls
- `Scanner.__init__`
- `Scanner.scan_all`
```

## Development

```bash
# Run tests
pytest

# Type checking
mypy src/

# Linting
ruff check src/

# Format
ruff format src/
```

## Roadmap

See [reference_architecture.md](file:///Users/deepg/Desktop/KnowCode/docs/architecture/reference_architecture.md) for the full vision and detailed architectural debt register.

**MVP (completed):**
- ✅ Single monorepo support
- ✅ Python, Markdown, YAML parsing
- ✅ Snapshot-only analysis (no temporal tracking)
- ✅ Local CLI tool

**Released:**
- ✅ v1.1: Additional languages (JavaScript, TypeScript, Java)
- ✅ v1.2: Git history integration, temporal tracking
- ✅ v1.3: Token budget optimization, priority ranking
- ✅ v1.4: Runtime signal integration
- ✅ v2.0: Intelligence Server mode (local API for local IDE agents)
- ✅ v2.1: Semantic search with embeddings, hybrid retrieval, and watch mode
- ✅ v2.2: Developer Q&A & IDE Agent Integration:
  - Query classification and task-specific templates
  - Multi-hop `trace_calls()` and impact analysis
  - Local-first `smart_answer()` with sufficiency scoring
  - MCP server for IDE integration
  - VoyageAI cross-encoder reranking

**v2.3 — Architectural Hardening (completed):**
- ✅ Modularise dependencies into optional extras (core install stays lightweight)
- ✅ Remove hidden side effects from query paths (fail fast, not auto-build)
- ✅ Schema versioning on persisted knowledge store and index artifacts
- ✅ Fix `metadata` type restriction (`dict[str, str]` → `dict[str, Any]`)
- ✅ Harden configuration loading (logging, validation, strict server mode)
- ✅ Decompose `KnowCodeService` and introduce `Protocol` interfaces
- ✅ Add layer contract tests and harden retrieval evals (parser, store roundtrip, golden-query smoke baseline — see [docs/retrieval-evals.md](retrieval-evals.md))

**Future releases:**
- v2.4: Multi-level documentation synthesis (in progress: architecture/module/function export + freshness manifest)
- v3.0: Deep analysis (data flow, intent extraction, confidence scoring)
- v4.0: Enterprise features (RBAC, scalability, team sharing)

## License

MIT
