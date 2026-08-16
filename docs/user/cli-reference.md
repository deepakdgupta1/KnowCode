# CLI Reference

Every KnowCode command, its flags, defaults, and examples. This page is the
single source of truth for the command surface — other docs link here rather
than restating flags or defaults.

KnowCode has **16 top-level commands**: `install`, `analyze`, `build`,
`index`, `query`, `semantic-search`, `context`, `export`, `stats`,
`telemetry`, `doctor`, `preflight`, `server`, `history`, `ask`, and
`mcp-server`.

Commands that require optional extras fail fast with an actionable hint
(e.g., `Install knowcode[server] to use 'knowcode server'.`). The extras
matrix and `knowcode install` are covered in
[Configuration](configuration.md#optional-extras).

---

## Lifecycle commands

### `install`

Install the dependencies for a full-featured KnowCode setup (equivalent to
the `search`, `llm`, `server`, `watch`, `mcp`, and `voyageai` extras). Useful
after a minimal `pip install knowcode`.

```bash
knowcode install [--upgrade] [--user] [--dry-run]
```

| Flag | Effect |
|---|---|
| `--upgrade` | Upgrade packages while installing |
| `--user` | Install into the Python user site-packages directory |
| `--dry-run` | Print the installer command without running it |

### `analyze`

Scan and parse a directory to build the knowledge store (semantic graph of
entities and relationships). Graph only — no semantic index.

```bash
knowcode analyze <directory> [--output <path>] [--ignore <pattern>] [--temporal] [--coverage <cobertura.xml>]
```

- `--temporal` — also extract git authorship/change history (enables the
  [`history`](#history) command and `authored`/`changed_by` relationships).
- `--coverage` — merge runtime coverage signals from a Cobertura XML report.

**Example:**
```bash
knowcode analyze src/ --ignore "tests/*" --temporal
```

### `build`

Build the knowledge base **and** the semantic index in one atomic step.
Recommended first command for any new repository. Prints entity/relationship
counts and the preflight report card; a failed build leaves the previous
generation in place.

```bash
knowcode build <directory> [--ignore <pattern>] [--config <path>] [--temporal] [--incremental]
```

**Example:**
```bash
knowcode build . --ignore "tests/*"
```

### `index`

Build (or update) the semantic search index — chunks, embeddings, BM25
tokens, vector store — published as part of a generation.

```bash
knowcode index <directory> [--output <path>] [--config <path>] [--incremental]
```

### `preflight`

Grade the target codebase across 10 quality dimensions (documentation
density, naming, parse success rate, …) and print a report card with
recommendations. Cheap: it reuses the parsed graph, never re-reads sources.
Also run automatically as part of `build`.

```bash
knowcode preflight <directory> [--ignore <pattern>] [--config <path>] [--json]
```

### `doctor`

Check whether the local setup is ready for daily use: strict config loading,
required API keys, knowledge store schema, semantic index schema and
embedding dimensions, artifact disk footprint (default threshold 500 MB),
unsupported-language warnings, freshness, and optionally a live MCP stdio
handshake.

```bash
knowcode doctor [--store <path>] [--index <path>] [--config <path>] [--max-disk-mb <n>] [--mcp] [--json]
```

**Example:**
```bash
knowcode doctor --store . --mcp
```

## Query commands

### `query`

Lexical queries against the knowledge store.

```bash
knowcode query <type> <target> [--store <path>] [--json]
```

**Query types:**

| Type | Purpose |
|---|---|
| `search <pattern>` | Search entities by name |
| `callers <entity>` | Find what calls this entity |
| `callees <entity>` | Find what this entity calls |
| `deps <entity>` | Get all dependencies |

**Example:**
```bash
knowcode query callers "GraphBuilder.build_from_directory"
```

### `semantic-search`

Natural-language search against the semantic index (hybrid BM25 + vectors).

```bash
knowcode semantic-search <query> [--index <path>] [--store <path>] [--config <path>] [--limit <n>]
```

`--limit` defaults to **5**.

**Example:**
```bash
knowcode semantic-search "Where is the graph built?"
```

### `context`

Generate a token-budgeted context bundle for one entity, ready for AI
consumption. Prints the bundle, its token count, and a truncation flag.

```bash
knowcode context <entity> [--store <path>] [--max-tokens <n>]
```

`--max-tokens` defaults to **2000**.

**Example:**
```bash
knowcode context "GraphBuilder.build_from_directory" --max-tokens 4000
```

### `ask`

Ask a natural-language question about the codebase. Classifies the query,
retrieves the minimal sufficient context, and answers using the first
available configured LLM (with failover). Requires `knowcode[llm]` and an
API key; the store and index must already exist.

```bash
knowcode ask <question> [--store <path>] [--config <path>]
```

**Example:**
```bash
knowcode ask "How does the graph builder work?"
```

### `history`

Show git history for the whole codebase or for one entity. Requires the
store to have been built with `--temporal`.

```bash
knowcode history [target] [--store <path>] [--limit <n>]
```

`--limit` defaults to **10**.

**Example:**
```bash
knowcode history "KnowledgeStore"
```

## Output commands

### `export`

Export the knowledge store as deterministic, no-LLM multi-level Markdown
documentation: an index, an architecture overview, one page per module, and
a `documentation_manifest.json` of entity content hashes for freshness
checks.

```bash
knowcode export [--store <path>] [--output <dir>]
```

`--output` defaults to **`docs`**.

### `stats`

Print entity and relationship counts by kind for the current store.

```bash
knowcode stats [--store <path>]
```

## Serving commands

### `server`

Start the local FastAPI intelligence server — the preferred surface for
locally hosted AI agents. Binds `127.0.0.1:8000` by default (loopback only;
there is no authentication). Requires `knowcode[server]`; `--watch` requires
`knowcode[watch]`.

```bash
knowcode server [--host <host>] [--port <port>] [--store <path>] [--watch]
```

**Example:**
```bash
knowcode server --port 8080 --watch
```

With `--watch`, modified/created files are queued for incremental
re-indexing and deleted/renamed files have their chunks invalidated. The
endpoint reference is in [REST API](rest-api.md).

### `mcp-server`

Start the MCP (Model Context Protocol) server over stdio for IDE agent
integration. Read-only and deterministic: tools never auto-run analysis.

```bash
knowcode mcp-server [--store <path>] [--config <path>]
```

**Tools exposed:** `search_codebase`, `get_entity_context`, `trace_calls`,
`retrieve_context_for_query`, `assess_codebase_quality`. Client setup for
Claude Desktop, VS Code, and Antigravity is covered in
[IDE & Agent Integration](ide-integration.md); the canonical retrieval
policy is the [MCP contract](../mcp-contract.md).

## Telemetry

### `telemetry show` / `telemetry clear`

Summarize or delete the local telemetry log. Telemetry never leaves your
machine and never contains your questions or code — see
[Telemetry & Privacy](telemetry.md).

```bash
knowcode telemetry show [--store <path>]
knowcode telemetry clear [--store <path>] [--yes]
```

---

## Supported language matrix

| Extension | Language | Parser mechanism | Notes |
|---|---|---|---|
| `.py` | Python | Python AST | Full semantic parsing (Python 3.10–3.12) |
| `.js`, `.jsx` | JavaScript | Tree-sitter | Classes, functions, imports, JSX tags |
| `.ts`, `.tsx` | TypeScript | Tree-sitter | Classes, functions, imports, TSX tags |
| `.java` | Java | Tree-sitter | Classes, methods, imports, inheritance |
| `.rs` | Rust | Tree-sitter | Structs, enums, functions, impl blocks |
| `.vue` | Vue | Tree-sitter | Vue Single-File Component scripts |
| `.md` | Markdown | Custom parser | Heading hierarchy and document structure |
| `.yaml`, `.yml` | YAML | Custom parser | Configuration keys with nested structure |

Any extension not listed (e.g., `.go`, `.cpp`, `.h`, `.swift`, `.rb`, `.php`,
`.css`, `.html`) is **ignored** during analyze/index/build. Per-parser
construct coverage (what exactly each parser extracts, and its known
limitations) is tracked in the
[parser construct matrix](../architecture/parser-construct-matrix.md).
