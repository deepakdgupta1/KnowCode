# KnowCode

Transform your codebase into an effective knowledge base that provides accurate, relevant context for AI coding assistants—using minimal tokens.

[![codecov](https://codecov.io/gh/deepakdgupta1/KnowCode/graph/badge.svg?token=placeholder)](https://codecov.io/gh/deepakdgupta1/KnowCode)

## Overview

KnowCode analyzes your codebase and builds a semantic graph of entities (functions, classes, modules) and their relationships (calls, imports, dependencies). This structured knowledge enables:

- **Accurate context synthesis** for AI assistants
- **Token-efficient** context generation (only what's needed)
- **Local-first** querying without LLM dependency
- **Traceability** back to source code

## Installation

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install KnowCode
pip install -e ".[dev]"
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

# 5. View statistics
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

## Supported Languages (MVP)

- **Python** (.py) - Full AST parsing with functions, classes, methods, calls, imports
- **JavaScript / TypeScript** (.js, .ts) - Classes, functions, imports (via tree-sitter)
- **Java** (.java) - Classes, methods, imports, inheritance (via tree-sitter)
- **Markdown** (.md) - Document structure with heading hierarchy
- **YAML** (.yaml, .yml) - Configuration keys with nested structure

## Architecture

KnowCode follows a layered architecture:

1. **Scanner** - Discovers files with gitignore support
2. **Parsers** - Language-specific parsing (Python AST, Markdown, YAML)
3. **Graph Builder** - Constructs semantic graph with entities and relationships
4. **Knowledge Store** - In-memory graph with JSON persistence
5. **Context Synthesizer** - Generates token-efficient context bundles
6. **CLI** - User interface for all operations

See [KnowCode.md](KnowCode.md) for the complete reference architecture.

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

See [KnowCode.md](KnowCode.md) for the full vision. The MVP focuses on:

- ✅ Single monorepo support
- ✅ Python, Markdown, YAML parsing
- ✅ Snapshot-only analysis (no temporal tracking)
- ✅ Local CLI tool

**Future releases:**
- v1.1: Additional languages (JavaScript, TypeScript, Java)
- v1.2: Git history integration, temporal tracking
- v1.3: Token budget optimization, priority ranking
- v1.4: Runtime signal integration
- v2.0: Server mode, team sharing, enterprise features

## License

MIT
