# KnowCode

Transform your codebase into an effective knowledge base that provides accurate, relevant context for AI coding assistants—using minimal tokens.

[![codecov](https://codecov.io/gh/deepakdgupta1/KnowCode/graph/badge.svg?token=placeholder)](https://codecov.io/gh/deepakdgupta1/KnowCode) [![CI/CD Pipeline](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml)


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

# Install KnowCode (with dev dependencies)
uv sync --dev

# Set API keys (only needed for the features you use; see aimodels.yaml)
export VOYAGE_API_KEY_1="..."   # embeddings + reranking (semantic search)
export OPENAI_API_KEY="..."     # embeddings (alternative to VoyageAI)
export GOOGLE_API_KEY_1="..."   # LLM (Gemini) for `knowcode ask`
```

## Quick Start

```bash
# 1. Analyze your codebase
knowcode analyze src/

# 2. Query the knowledge store
knowcode query search "MyClass"
knowcode query callers "my_function"
knowcode query callees "MyClass.method"

# 3. Generate context for an entity
knowcode context "MyClass.important_method"

# 4. Export documentation
knowcode export -o docs/

# 5. Build semantic search index
knowcode index src/

# 6. Perform semantic search
knowcode semantic-search "How does parsing work?"

# 7. Start the intelligence server with watch mode
knowcode server --port 8080 --watch

# 8. View statistics
knowcode stats
```

## Commands

### `analyze`
Scan and parse a directory to build the knowledge store.

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
knowcode context <entity> [--store <path>] [--max-chars <n>]
```

**Example:**
```bash
knowcode context "GraphBuilder.build_from_directory" --max-chars 4000
```

### `export`
Export the knowledge store as Markdown documentation.

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
knowcode mcp-server [--store <path>]
```

**Tools Exposed:**
- `search_codebase` - Search for code entities by name
- `get_entity_context` - Get detailed context for an entity
- `trace_calls` - Trace call graph (callers/callees) with depth

## Supported Languages (MVP)

- **Python** (.py) - Full AST parsing (Supports Python 3.9 - 3.12)
- **JavaScript / TypeScript** (.js, .ts) - Classes, functions, imports (via tree-sitter)
- **Java** (.java) - Classes, methods, imports, inheritance (via tree-sitter)
- **Markdown** (.md) - Document structure with heading hierarchy
- **YAML** (.yaml, .yml) - Configuration keys with nested structure

## Architecture

KnowCode follows a layered architecture:

1. **Scanner** - Discovers files with gitignore support
2. **Parsers** - Language-specific parsing (Python AST, Tree-sitter for others)
3. **Graph Builder** - Constructs semantic graph with entities and relationships
4. **Knowledge Store** - In-memory graph with JSON persistence
5. **Indexer** - Vector embedding and hybrid retrieval engine (FAISS + BM25)
6. **Context Synthesizer** - Generates token-efficient context bundles with priority ranking
7. **CLI** - User interface for all operations

See [evolution.md](evolution.md) for the complete reference architecture.

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

See [evolution.md](evolution.md) for the full vision. The MVP focuses on:

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

**Future releases:**
- v3.0: Team sharing & Enterprise features (RBAC, SSO, etc.)

## License

MIT
