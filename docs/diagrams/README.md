# KnowCode Diagrams & Workflow Narrations

This directory contains the visual `.drawio` diagram assets along with their respective textual narrations. Below is the unified index of all system workflows and architecture details:

- [System Architecture Overview](#system-architecture-overview)
- [Indexing & Analysis Workflow](#indexing-analysis-workflow)
- [Query & Retrieval Workflow](#query-retrieval-workflow)
- [MCP Server Workflow](#mcp-server-workflow)
- [File Watch & Hot-Reload Workflow](#file-watch-hot-reload-workflow)
- [Agent Gateway Workflow](#agent-gateway-workflow)

---

## System Architecture Overview


> Textual narration of [`architecture_overview.drawio`](architecture_overview.drawio).
> Every component, relationship, and label in the draw.io file is described here in full.

---

## Overview

KnowCode is a code intelligence system that parses a codebase into a semantic knowledge graph, indexes it with hybrid BM25 + vector search, and exposes that intelligence through four distinct interfaces: a CLI, a REST API, an MCP server, and an Agent Gateway. The system is structured into five horizontal layers plus a separately deployable Agent Gateway microservice.

---

## Layer 0 — User Interfaces

All user-facing entry points sit in this layer. Every interface ultimately delegates to the Service Layer beneath it.

### CLI (`cli.py`, click framework)

The command-line interface exposes thirteen commands:

| Command | Purpose |
|---|---|
| `analyze` | Scan a directory, build knowledge graph, and auto-build semantic index |
| `build` | Build the knowledge base and semantic index for a directory |
| `index` | (Re)build the semantic index from an existing graph |
| `query` | Lexical query: callers, callees, dependencies, or search |
| `context` | Generate a task-aware context bundle for an entity |
| `semantic-search` | Natural-language search over embeddings |
| `export` | Export multi-level Markdown documentation from the knowledge graph |
| `stats` | Print entity and relationship counts |
| `doctor` | Check whether the local KnowCode setup is ready |
| `server` | Start the FastAPI REST server (optionally with `--watch`) |
| `history` | Show git commit history or entity change history |
| `ask` | Answer a question using the LLM Agent |
| `mcp-server` | Start the MCP server over STDIO |

### FastAPI REST API (`:8000`, uvicorn)

Eleven endpoints grouped by rate-limit tier:

**Standard (60 req/min):**
- `GET  /api/v1/health` — liveness check
- `GET  /api/v1/stats` — entity/relationship counts
- `GET  /api/v1/search?q=` — lexical entity search
- `GET  /api/v1/context?target=&task_type=` — context bundle for a named entity
- `GET  /api/v1/entities/{entity_id}` — raw entity detail
- `GET  /api/v1/callers/{entity_id}` — direct callers
- `GET  /api/v1/callees/{entity_id}` — direct callees
- `POST /api/v1/context/query` — semantic query with retrieval orchestration
- `POST /api/v1/reload` — reload KnowledgeStore from disk

**Expensive (10 req/min):**
- `GET  /api/v1/trace_calls/{entity_id}?direction=&depth=` — multi-hop BFS traversal
- `GET  /api/v1/impact/{entity_id}?max_depth=` — transitive impact analysis

### MCP Server (STDIO, JSON-RPC 2.0)

Used by Claude Desktop and compatible IDEs. Exposes four tools:

1. `search_codebase(query, limit=10)`
2. `get_entity_context(entity_id, task_type, max_tokens)`
3. `trace_calls(entity_id, direction, depth)`
4. `retrieve_context_for_query(query, task_type, max_tokens, limit_entities, expand_deps, verbosity)`

### Agent Gateway (External Microservice)

The Agent Gateway was previously hosted in this repository but has since been extracted to a separate repository. It is a separately deployable microservice that proxies to the KnowCode REST API and wraps it in an LLM-driven tool-use loop via LiteLLM.

### API Rate Limiter (`rate_limit.py`, slowapi, IP-keyed)

Attached to the FastAPI app as middleware. Two tiers:
- **Standard:** 60 requests/minute — all endpoints except trace and impact
- **Expensive:** 10 requests/minute — `trace_calls`, `impact`

---

## Layer 1 — Service Layer

### `KnowCodeService` (`service.py`)

The single central orchestrator. All interfaces call this class. Key public methods:

| Method | What it does |
|---|---|
| `analyze(dir, output, temporal, coverage)` | Builds knowledge graph via `GraphBuilder` → saves JSON → auto-calls `_build_index()`. Returns stats dict. |
| `ensure_store()` / `ensure_index()` | Build store or index only if not already present on disk |
| `get_indexer()` | Lazy-init `Indexer(embedding_provider)`, optionally load existing index |
| `get_search_engine()` | Lazy-init `SearchEngine(chunk_repo, embedding_provider, HybridIndex, store)` |
| `retrieve_context_for_query(query, max_tokens, task_type, limit_entities, expand_deps, verbosity)` | Delegates to `RetrievalOrchestrator` |
| `search(pattern)` | Lexical entity search on `KnowledgeStore` |
| `get_context(target, max_tokens, task_type)` | Single-entity context bundle via `ContextSynthesizer` |
| `get_callers(id)` / `get_callees(id)` | Graph traversal shortcuts |
| `get_entity_details(id)` | Raw entity dict |
| `get_stats()` | Entity/relationship/chunk/vector counts |
| `reload()` | Clears in-memory `_store`, re-reads from disk on next access |

The `store` property is lazy: it loads `KnowledgeStore` from disk on first access and caches it as `_store`.

---

## Layer 2 — Core Processing Pipelines

### Indexing Pipeline

Three components form a linear chain: **GraphBuilder → Chunker → Indexer**.

**`GraphBuilder` (`graph_builder.py`)**

- `build_from_directory(root_dir, additional_ignores, analyze_temporal, coverage_path)` — orchestrates the full scan:
  1. Calls `Scanner.scan(root_dir)` to discover files (applying `.gitignore` via pathspec)
  2. For each `FileInfo`, calls `_parse_file()` which selects the correct parser by language
  3. Accumulates `ParseResult` objects via `_merge_result()`
  4. After all files: calls `_resolve_references()` to wire cross-file `CALLS`, `IMPORTS`, `INHERITS` relationships
  5. Optionally runs `TemporalAnalyzer` (git history) and `CoverageProcessor` (Cobertura XML)
- Exposes: `get_entity()`, `get_entities_by_kind()`, `search_entities()`, `stats()`

**`Chunker` (`chunker.py`)**

- `process_parse_result(result)` — splits each entity into overlapping `CodeChunk` objects:
  - Module header chunks (file-level docstring + metadata)
  - Import block chunk
  - Per-entity chunks (signature + docstring + body)
  - Each chunk carries BM25 tokenized `tokens[]` list

**`Indexer` (`indexer.py`)**

- `index_directory(directory)` — runs its own internal scan+parse+chunk+embed pipeline end-to-end
- `index_file(file_path)` — incremental re-index of a single file (used by `BackgroundIndexer`)
- `save(index_path)` — writes `chunks.json`, `vectors.index`, `vectors.json` under `knowcode_index/`
- `load(index_path)` — restores from disk

> **Note:** `KnowCodeService.analyze()` calls `GraphBuilder` for the knowledge graph, then separately calls `_build_index()` which creates a new `Indexer` that scans again. Both pipelines run during `knowcode analyze`.

### Retrieval Pipeline

Five components: **query_classifier.py → HybridIndex → SearchEngine → Reranker → expand_dependencies**.

**`query_classifier.py` (Module)**

- `classify_query(query)` → `(TaskType, confidence: float)`
- Uses regex pattern matching with weighted scoring across five task types: `EXPLAIN`, `DEBUG`, `EXTEND`, `REVIEW`, `LOCATE`
- Returns `GENERAL` with confidence 0.0 when no patterns match
- Also provides `get_prompt_template(task_type)` — task-specific LLM system prompt strings

**`HybridIndex` (`hybrid_index.py`)**

- `search(query_text, query_vector, limit)` → `list[(CodeChunk, score)]`
- Combines:
  - BM25 lexical search on `ChunkRepository` token lists
  - FAISS dense similarity search on `VectorStore` (cosine via `IndexFlatIP` with normalized vectors)
  - Merges and normalizes scores from both retrieval modes

**`SearchEngine` (`search_engine.py`)**

- `search_scored(query, limit, expand_deps)` → `list[ScoredChunk]` — the full pipeline:
  1. `embedding_provider.embed_single(query)` → query vector
  2. `hybrid_index.search(query, query_vector, limit×2)`
  3. `reranker.rerank(query, results, top_k=limit)`
  4. `expand_dependencies()` for each top result
- `search(query, limit, expand_deps)` → `list[CodeChunk]` (strips scores)
- `ScoredChunk` carries `{chunk, score, source: "retrieved"|"dependency"}`

**`Reranker` (`reranker.py`)**

- `rerank(query, chunks, top_k)` → `list[(CodeChunk, score)]`
- **Primary path:** VoyageAI cross-encoder (`rerank-2.5` model via `voyage_client.rerank()`)
- **Fallback path** (if VoyageAI unavailable): signal-based scoring:
  - `boost_documented`: ×1.2 if chunk has docstring
  - `boost_recent`: ×1.1 if last-modified within 7 days
  - Query-in-content: ×1.5 if query string appears in chunk text
  - Exact kind match: ×2.0

**`expand_dependencies` (`completeness.py`)**

- Takes a `CodeChunk` and expands to include its callees (up to `max_depth=1`)
- Uses `chunk_repo.get_by_entity()` + `knowledge_store.get_callees()`
- Marks expanded chunks with `source="dependency"`

### `RetrievalOrchestrator` (`retrieval/orchestrator.py`)

Coordinates the full end-to-end retrieval flow:

1. Validate store + index exist
2. `classify_query()` → resolve task type (override if caller specified one)
3. `get_search_engine()` → validate index compatibility (embedding dimension + model)
4. `engine.search_scored()` → semantic retrieval (falls back to lexical on any exception)
5. For each selected entity: `get_context()` → `ContextSynthesizer`
6. Assemble `context_text`, compute average `sufficiency_score`
7. Filter response fields based on `verbosity`:
   - `minimal` → `{context_text, sufficiency_score, total_tokens, reduction_summary}`
   - `standard` → + `query, task_type, task_confidence, retrieval_mode, max_tokens, truncated`
   - `verbose` → + `evidence[]`
   - `diagnostic` → full dict with all fields and `errors[]`

---

## Layer 2b — LLM Agent

### `ContextSynthesizer` (`analysis/context_synthesizer.py`)

Generates token-budget-aware context bundles for individual entities.

- `synthesize(entity_id, summarize)` — default synthesis: header + docstring + signature + source_code + parent + callers + callees + children (in priority order, stopping at token budget)
- `synthesize_with_task(entity_id, task_type, summarize)` — task-prioritized synthesis using `TASK_TEMPLATES`:

| TaskType | Priority order | Boosts |
|---|---|---|
| `DEBUG` | source_code, callers, callees, signature, docstring | source_code ×2.0, callers ×1.5 |
| `EXTEND` | signature, docstring, children, parent, source_code | signature ×1.5, children ×1.3 |
| `REVIEW` | source_code, callers, callees, signature | callers ×1.5, callees ×1.5 |
| `EXPLAIN` | docstring, signature, source_code, callees, parent | docstring ×1.5, callees ×1.3 |
| `LOCATE` | signature, docstring, parent | none |
| `GENERAL` | docstring, signature, source_code, parent, callers, callees | none |

- `_calculate_sufficiency(task_type, content_included, entity, text)` → `float 0.0–1.0`
  - Weighted sum over priority sections (weight = 1/(rank+1))
  - Bonus: +0.2 if source_code included; +0.1 if long docstring present
  - Penalty: ×0.5 if total context < 100 chars
- Returns `ContextBundle {target_entity, context_text, included_entities, total_tokens, truncated, task_type, sufficiency_score}`

### `Agent` (`llm/agent.py`)

Answers codebase questions using configured LLM providers.

- `answer(query)` — always invokes an LLM:
  1. `service.retrieve_context_for_query(query)` → context bundle
  2. `get_prompt_template(task_type)` → system instructions
  3. Iterate configured models with RPM/RPD rate-limit check:
     - Google: `client.models.generate_content(model, prompt)`
     - OpenAI-compatible: `client.chat.completions.create(model, messages)` (with `HTTP-Referer` header for OpenRouter)
     - On `ResourceExhausted` or error: try next model
  4. `rate_limiter.record_usage(model.name)` → `~/.knowcode/usage_stats.json`

- `smart_answer(query, force_llm=False)` — local-first:
  1. Retrieve context and check `sufficiency_score ≥ config.sufficiency_threshold` (default 0.8)
  2. If sufficient: `_format_local_answer()` — returns context-only answer (zero LLM tokens)
  3. If insufficient or `force_llm=True`: delegates to `answer()`
  4. Returns `{answer, source: "local"|"llm", task_type, sufficiency_score, context, llm_tokens_saved}`

---

## Layer 3 — Storage Layer

### `KnowledgeStore` (`storage/knowledge_store.py`)

- In-memory semantic graph: `entities: dict[str, Entity]` + `relationships: list[Relationship]`
- Persistence: `knowcode_knowledge.json` (schema v2)
- Core factory: `from_graph_builder(builder)` — transfers parsed data into the store
- Persistence: `save(path)` / `load(path)` / `_migrate_schema()` (handles v1→v2 upgrade)
- Graph queries: `search()`, `get_entity()`, `get_callers()`, `get_callees()`, `get_children()`, `get_parent()`, `get_dependencies()`, `get_dependents()`, `trace_calls()`, `get_impact()`, `list_by_kind()`

### `VectorStore` (`storage/vector_store.py`)

- Wraps FAISS `IndexFlatIP` with L2-normalized embeddings (equivalent to cosine similarity)
- Default embedding dimension: 1024 (voyage-code-3)
- Persistence: `knowcode_index/vectors.index` (FAISS binary) + `knowcode_index/vectors.json` (metadata)
- API: `add(chunks, embeddings)`, `search(query_vector, k)`, `save()`, `load()`, `clear()`, `_validate_and_migrate_metadata()`

### `ChunkRepository` (`storage/chunk_repository.py`)

- `InMemoryChunkRepository` implementation
- Stores `CodeChunk` objects indexed by `chunk_id` and `entity_id`
- Persistence: `knowcode_index/chunks.json`
- API: `add(chunk)`, `get(chunk_id)`, `get_by_entity(entity_id)`, `search_by_tokens(tokens)` (BM25 candidate lookup), `clear()`

---

## Layer 4 — Infrastructure / Plugins

### Parsers (`parsers/`)

Eight parser implementations, all extending `TreeSitterParser` (base class):

| Parser | Language |
|---|---|
| `PythonParser` | Python |
| `JavaScriptParser` | JavaScript |
| `TypeScriptParser` | TypeScript |
| `JavaParser` | Java |
| `RustParser` | Rust |
| `VueParser` | Vue SFCs |
| `MarkdownParser` | Markdown (docs) |
| `YamlParser` | YAML configs |

Each implements `_extract_entities()` and returns `ParseResult {entities[], relationships[], errors[]}`. The base class handles Tree-sitter `parse_file()`, `_get_text()`, `_get_location()`, `_create_entity()`.

### EmbeddingProviders (`llm/embedding.py`)

Abstract base `EmbeddingProvider` with `embed(texts[])` and `embed_single(text)` methods.

- `VoyageAIEmbeddingProvider` — uses `voyage-code-3` (dim=1024), distinguishes `input_type=document` (indexing) vs `input_type=query` (search)
- `OpenAIEmbeddingProvider` — supports `text-embedding-3-small` (1536-dim) and `text-embedding-3-large` (3072-dim)
- `create_embedding_provider(app_config)` factory: tries each configured embedding model in order, checks API key availability, falls back to VoyageAI default

### LLM Clients (`llm/agent.py`)

- `_create_google_client(api_key)` → `google.genai.Client`
- `_create_openai_client(api_key, base_url)` → `openai.OpenAI` (with optional base_url override for OpenRouter/Mistral)
- Model failover order defined in `AppConfig.models` (loaded from `aimodels.yaml`)

### Scanner (`indexing/scanner.py`)

- `scan(root_dir)` → `list[FileInfo]` — discovers all non-ignored files
- `_load_gitignore()` — reads `.gitignore` via pathspec
- `_should_ignore(path)` — applies gitignore rules + extension filter
- `FileInfo`: `{path, size, modified, language}` — language auto-detected from extension

### FileMonitor + BackgroundIndexer

**`FileMonitor` (`indexing/monitor.py`)**
- Wraps watchdog `Observer`
- `IndexingHandler.on_modified/on_created/on_deleted/on_moved(event)` → `Scanner.is_indexable(path)` → `bg_indexer.queue_file/queue_removal/queue_move(...)`
- `start()` / `stop()` — both idempotent; a second `start()` returns `False` rather than scheduling a second observer

**`WatchQueue` (`indexing/watch_queue.py`)**
- One `WatchWork` item per canonical file identity; the last event for a path wins
- A move is stored as its two effects — index the destination, drop the sources — so chained renames collapse into one commit
- Submission order is preserved, and an in-flight commit never absorbs a new event

**`BackgroundIndexer` (`indexing/background_indexer.py`)**
- One daemon thread draining a `WatchQueue`
- `queue_file` / `queue_removal` / `queue_move` — coalesced into the queue; raise `WatchQueueClosed` after `stop()`
- `_commit(work)` — Step 15 transactions: `indexer.replace_file(...)` or `delete_file(...)`, then the dropped sources
- Retryable failures back off and retry up to `max_attempts`; terminal and exhausted ones land in `failures()` with the file's previous generation intact
- `start()` / `stop(timeout)` — idempotent; `stop` drains within one deadline and returns a `DrainReport` naming any uncommitted work

### TemporalAnalyzer + CoverageProcessor

**`TemporalAnalyzer` (`analysis/temporal.py`)**
- `analyze_history(limit=100)` — uses GitPython to parse commit log
- Creates `COMMIT` and `AUTHOR` entities with `AUTHORED`, `MODIFIED`, `CHANGED_BY` relationships
- Stores `insertions`, `deletions` metadata on `MODIFIED` relationships

**`CoverageProcessor` (`analysis/signals.py`)**
- `process_cobertura(xml_path)` — parses Cobertura XML coverage report
- Creates `COVERAGE_REPORT` entity and `COVERS` relationships linking the report to covered modules

### Config (`config.py`)

- `AppConfig.load()` — priority: explicit path → `./aimodels.yaml` → `~/.aimodels.yaml` → defaults
- `ModelConfig {name, provider, api_key_env, rpm_free_tier_limit=10, rpd_free_tier_limit=1000}`
- Defaults: NL models = `[gemini-2.0-flash-lite, gemini-1.5-flash, gemini-1.5-pro]`; embedding = `voyage-code-3`; `sufficiency_threshold = 0.8`

---

## Key Data Models

### `Entity`
```
id: "file_path::qualified_name"
kind: EntityKind  (MODULE|CLASS|FUNCTION|METHOD|VARIABLE|DOCUMENT|SECTION|CONFIG_KEY|COMMIT|AUTHOR|TEST_RUN|COVERAGE_REPORT)
name, qualified_name, location: Location{file_path, line_start, line_end, column_start, column_end}
docstring, signature, source_code, metadata: dict
```

### `Relationship`
```
source_id, target_id, kind: RelationshipKind, metadata: dict
RelationshipKind: CALLS|IMPORTS|CONTAINS|INHERITS|IMPLEMENTS|USES_TYPE|REFERENCES
                  CHANGED_BY|AUTHORED|MODIFIED  (temporal)
                  COVERS|EXECUTED_BY  (runtime)
```

### `CodeChunk`
```
id: "entity_id::chunk_index"
entity_id, content, tokens: list[str], embedding: list[float] | None, metadata: dict
```

### `EmbeddingConfig` (default)
```
provider: "voyageai", model_name: "voyage-code-3", dimension: 1024, batch_size: 100, normalize: True
```

---

## Cross-Layer Arrows Summary

| From | To | Nature |
|---|---|---|
| CLI / REST API / MCP | KnowCodeService | synchronous call |
| Agent Gateway | KnowCode REST API | HTTP (dashed) |
| KnowCodeService | GraphBuilder / SearchEngine / RetrievalOrchestrator / ContextSynthesizer / Agent | delegation |
| Indexer | KnowledgeStore / VectorStore / ChunkRepository | writes |
| SearchEngine | VectorStore / ChunkRepository | reads (dashed) |
| ContextSynthesizer | KnowledgeStore | reads (dashed) |
| GraphBuilder | Scanner / Parsers | uses |
| Indexer | EmbeddingProviders | uses |
| Agent | LLM Clients | calls |
| Reranker | EmbeddingProviders (VoyageAI) | uses (dashed) |
| REST API | API Rate Limiter | uses (dashed) |
| REST API | FileMonitor | triggers (dashed, watch mode) |


---


## Indexing & Analysis Workflow


> Textual narration of [`seq_indexing.drawio`](seq_indexing.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode analyze <dir>`  
**Side effect:** automatically builds the semantic index (no separate `knowcode index` call needed after analyze)

---

## Participants

| Participant | File | Role |
|---|---|---|
| User / CI | — | Invokes `knowcode analyze` |
| CLI | `cli/cli.py` | Parses arguments, calls service |
| KnowCodeService | `service.py` | Central orchestrator |
| GraphBuilder | `indexing/graph_builder.py` | Parses codebase into entity/relationship graph |
| Scanner | `indexing/scanner.py` | File discovery with gitignore filtering |
| Parser (×8 langs) | `parsers/` | Language-specific AST extraction |
| KnowledgeStore | `storage/knowledge_store.py` | In-memory graph + JSON persistence |
| Indexer | `indexing/indexer.py` | Full scan→chunk→embed pipeline |
| Chunker | `indexing/chunker.py` | Splits entities into BM25-tokenized code chunks |
| EmbeddingProvider | `llm/embedding.py` | Converts text to dense vectors |
| VectorStore + ChunkRepo | `storage/vector_store.py`, `storage/chunk_repository.py` | Persists vectors (FAISS) and chunks (JSON) |

---

## Phase 1 — Knowledge Graph Construction

### Step 1 — User invokes analyze

```
User → CLI:  knowcode analyze ./src [--temporal] [--coverage=report.xml]
```

Optional flags:
- `--temporal` — enables git history analysis
- `--coverage=<path>` — enables Cobertura XML coverage ingestion

### Step 2 — CLI delegates to service

```
CLI → KnowCodeService:  service.analyze(directory, output, ignore, temporal, coverage)
```

`output` defaults to the same directory as `directory`, producing `knowcode_knowledge.json` in place.

### Step 3 — GraphBuilder instantiated and scan begins

```
KnowCodeService → GraphBuilder:  GraphBuilder()
                                 builder.build_from_directory(root_dir, additional_ignores, analyze_temporal, coverage_path)
```

`build_from_directory` is the top-level entry point for the knowledge graph pipeline.

### Step 4 — Scanner discovers files

```
GraphBuilder → Scanner:  Scanner.scan(root_dir)
Scanner returns:  list[FileInfo]  {path, size, modified, language}
```

The scanner:
- Loads `.gitignore` rules via `pathspec`
- Applies `_should_ignore(path)` filter (extension list + gitignore patterns)
- Returns one `FileInfo` per qualifying file, with language auto-detected from extension

### Step 5 — [Loop] Parse each file

For each `FileInfo` in the discovered list:

```
GraphBuilder → Parser:  _parse_file(file_info)  →  select parser by language
Parser:                  parse_file(file_path, source)  →  AST traversal
Parser returns:          ParseResult  {entities[], relationships[], errors[]}
```

Language-specific parsers (Python, JavaScript, TypeScript, Java, Rust, Vue, Markdown, YamlParser) extend `TreeSitterParser`. Each parser:
- Parses source with Tree-sitter
- Extracts entities (functions, classes, methods, variables, modules)
- Records intra-file relationships (CALLS, IMPORTS, CONTAINS, INHERITS)

```
GraphBuilder:  _merge_result(parse_result)  →  accumulate entities + relationships into internal collections
```

### Step 6 — End of file loop

### Step 7 — Resolve cross-file references

```
GraphBuilder:  _resolve_references()
```

After all files are parsed, GraphBuilder resolves cross-file relationships:
- CALLS edges: function calls resolved by qualified name across modules
- IMPORTS edges: import statements linked to the imported module entity
- INHERITS edges: class inheritance resolved by name lookup

### Step 8 — Optional temporal analysis

```
GraphBuilder:  [if --temporal]  TemporalAnalyzer.analyze_history(limit=100)
```

- Uses GitPython to read commit log
- Creates `COMMIT` and `AUTHOR` entities
- Creates `AUTHOR→AUTHORED→COMMIT`, `COMMIT→MODIFIED→MODULE`, `MODULE→CHANGED_BY→COMMIT` relationships
- Stores `insertions`, `deletions` as relationship metadata

### Step 9 — Optional coverage analysis

```
GraphBuilder:  [if --coverage]  CoverageProcessor.process_cobertura(xml_path)
```

- Parses Cobertura XML report
- Creates `COVERAGE_REPORT` entity
- Creates `COVERAGE_REPORT→COVERS→MODULE` relationships with `line_rate` metadata

### Step 10 — Build and save KnowledgeStore

```
KnowCodeService → KnowledgeStore:  KnowledgeStore.from_graph_builder(builder)
KnowledgeStore:  store.save(output_path)  →  writes knowcode_knowledge.json  (schema v2)
KnowledgeStore returns to KnowCodeService:  KnowledgeStore instance  (cached as service._store)
```

The JSON file structure:
```json
{
  "schema_version": 2,
  "version": "1.0",
  "metadata": {"stats": {…}, "errors": []},
  "entities": {"entity_id": {…Entity…}},
  "relationships": [{…Relationship…}]
}
```

---

## Phase 2 — Semantic Index Build

Called automatically by `service.analyze()` immediately after saving the knowledge store. Can also be called independently via `knowcode index`.

### Step 11 — Build index invoked

```
KnowCodeService:  service._build_index(directory, index_path)
```

`index_path` defaults to `<store_root>/knowcode_index/`.

### Step 12 — Create embedding provider

```
KnowCodeService:  create_embedding_provider(app_config)
```

Factory logic:
1. Try each model in `app_config.embedding_models` in order
2. Check API key is set in environment
3. Return `VoyageAIEmbeddingProvider(voyage-code-3, dim=1024)` (default) or `OpenAIEmbeddingProvider`

### Step 13 — Indexer runs full scan

```
KnowCodeService → Indexer:  Indexer(embedding_provider)
                             indexer.index_directory(directory)
```

The Indexer **runs its own internal scan + parse + chunk pipeline** (independent of the GraphBuilder scan above). This means files are scanned twice during `knowcode analyze` — once for the knowledge graph and once for the semantic index.

Internally, `index_directory` uses Scanner + GraphBuilder to re-parse, then hands results to Chunker.

### Step 14 — Chunker produces code chunks

```
Indexer → Chunker:  Chunker.process_parse_result(result)
Chunker returns:  CodeChunk[]  {id, entity_id, content, tokens[], metadata}
```

For each parsed entity, the Chunker produces:
- A **module header chunk**: file path, docstring, top-level summary
- An **import block chunk**: all import statements concatenated
- **Entity chunks** (overlapping if the entity is large): signature + docstring + body, with configurable `max_chunk_size=1000` and `overlap=100` tokens

Each chunk carries BM25-tokenized `tokens[]` for lexical search.

### Step 15 — [Loop] Embed chunks in batches

```
Indexer → EmbeddingProvider:  EmbeddingProvider.embed(texts[])  →  VoyageAI / OpenAI API call
EmbeddingProvider returns:  list[list[float]]  (dim=1024, L2-normalized)
```

Batching: `batch_size=100` chunks per API call. Embeddings are L2-normalized to enable cosine similarity via FAISS `IndexFlatIP`.

### Step 16 — Store chunks and vectors

```
Indexer → ChunkRepository:  ChunkRepository.add(chunks)
Indexer → VectorStore:      VectorStore.add(chunks, embeddings)
VectorStore:                 builds FAISS IndexFlatIP  (inner product on normalized = cosine)
```

### Step 17 — Persist index to disk

```
Indexer:  indexer.save(index_path)
          → chunks.json         (all CodeChunk objects)
          → vectors.index       (FAISS binary index)
          → vectors.json        (metadata: schema version, embedding dimension, model name)
```

### Step 18 — Return stats to CLI

```
Indexer returns to KnowCodeService:  indexed_chunks count
KnowCodeService returns to CLI:      stats dict {entities, relationships, indexed_chunks, index_path, [index_error]}
CLI → User:                          print summary (entity counts, relationship types, index size)
```

If `_build_index()` raises an exception (e.g., missing API key), `index_error` is included in stats but the overall `analyze` command still succeeds (knowledge graph was saved).

---

## Optional: File Watch Mode

When `knowcode server --watch` is running:

- `FileMonitor` (watchdog `Observer`) watches the project directory
- On file save: `IndexingHandler.on_modified()` or `on_created()` → `Scanner.is_indexable(path)` → `bg_indexer.queue_file(path)`
- `WatchQueue` coalesces by canonical identity, so an editor's burst of events for one file becomes one work item
- `BackgroundIndexer._worker()` (daemon thread): takes one item, commits it as a Step 15 transaction (`replace_file` / `delete_file`), retries retryable failures with backoff
- That transaction re-runs steps 14–17 for the single changed file only (incremental, not full re-scan), and a failure leaves the file's previous generation searchable
- After re-index: the next API request automatically sees fresh data (no server restart needed)

`POST /api/v1/reload` clears the in-memory `KnowledgeStore` cache; on next access it re-reads `knowcode_knowledge.json` from disk.


---


## Query & Retrieval Workflow


> Textual narration of [`seq_query_retrieval.drawio`](seq_query_retrieval.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode context` · `knowcode ask` · REST `POST /api/v1/context/query` · MCP `retrieve_context_for_query`

---

## Participants

| Participant | File | Role |
|---|---|---|
| User / Agent | — | Issues query or question |
| CLI / REST / MCP | `cli/cli.py`, `api/api.py`, `mcp/server.py` | Entry point — routes to KnowCodeService |
| KnowCodeService | `service.py` | Central orchestrator |
| RetrievalOrchestrator | `retrieval/orchestrator.py` | Validates, classifies, retrieves, synthesizes |
| query_classifier.py (Module) | llm/query_classifier.py | Detects task type via regex pattern matching |
| SearchEngine | `retrieval/search_engine.py` | Embeds query, calls HybridIndex, reranks |
| HybridIndex | `retrieval/hybrid_index.py` | Merges BM25 (lexical) + FAISS (dense) results |
| Reranker | `retrieval/reranker.py` | Cross-encoder reranking (VoyageAI primary, signal fallback) |
| expand\_dependencies | `retrieval/completeness.py` | Expands callee context for top-ranked chunks |
| ContextSynthesizer | `analysis/context_synthesizer.py` | Builds ContextBundle; computes sufficiency score |
| Agent / LLM (ask cmd) | `llm/agent.py` | Generates natural language answer (Alt B only) |

---

## Step 1 — User invokes query entry point

```
User → CLI/REST/MCP:  query / question / entity_id
```

The caller uses one of four entry points:
- `knowcode context <query>` — CLI, returns structured context
- `knowcode ask <question>` — CLI, returns LLM-generated answer
- `POST /api/v1/context/query` — REST API (`QueryRequest`)
- `retrieve_context_for_query` — MCP tool call

## Step 2 — Entry point calls service

```
CLI/REST/MCP → KnowCodeService:
  service.retrieve_context_for_query(
    query, max_tokens=4000, task_type,
    limit_entities=3, expand_deps, verbosity
  )
```

## Step 3 — Service delegates to orchestrator

```
KnowCodeService → RetrievalOrchestrator:
  orchestrator.retrieve_context_for_query(…)
```

## Step 4 — Validate preconditions

```
RetrievalOrchestrator:  _assert_store_exists()  +  _assert_index_exists()
```

Raises HTTP 412 if the knowledge store or semantic index has not been built yet.

## Step 5 — Classify query

```
RetrievalOrchestrator → query_classifier.py:  classify_query(query)
```

The classifier uses five sets of weighted regex patterns (one per `TaskType`):
- `EXPLAIN`, `DEBUG`, `EXTEND`, `REVIEW`, `LOCATE`

Returns: `(TaskType, confidence)`.

`resolved_task_type = task_type override (if caller supplied) OR detected task_type`

## Step 6 — Lazy-init search engine

```
RetrievalOrchestrator:
  service.get_search_engine()
  → HybridIndex(chunk_repository, vector_store)  [created once, cached]
```

## Step 7 — Validate index compatibility

```
RetrievalOrchestrator:
  _validate_index_compatibility(index_path)
  → checks embedding dimension + model name match
  → raises on mismatch
```

## Step 8 — Search: retrieve scored chunks

```
RetrievalOrchestrator → SearchEngine:
  engine.search_scored(query, limit=max(10, limit_entities×5), expand_deps)
```

### Step 9 — Embed query

```
SearchEngine:
  embedding_provider.embed_single(query)  →  query_vector  (dim=1024)
```

### Step 10 — Hybrid search

```
SearchEngine → HybridIndex:  hybrid_index.search(query, query_vec, limit=limit×2)
```

Internally HybridIndex executes three sub-steps:

- **10a** — BM25 search on `ChunkRepository` token lists (lexical)
- **10b** — FAISS similarity search on `VectorStore` (`IndexFlatIP`, cosine similarity via L2-normalized inner product)
- **10c** — Merge + normalize scores → `list[(CodeChunk, score)]`

Returns: top `limit×2` candidates back to SearchEngine.

### Step 11 — Rerank

```
SearchEngine → Reranker:  reranker.rerank(query, results, top_k=limit)
```

- **Primary**: VoyageAI `rerank-2.5` cross-encoder
- **Fallback** (if VoyageAI unavailable): signal-based scoring:
  - `boost_documented × 1.2`
  - `boost_recent × 1.1`
  - query text found in content: `× 1.5`
  - exact entity kind match: `× 2.0`

Returns: `list[(CodeChunk, score)]` top\_k reranked.

### Step 12 — Expand dependencies

```
SearchEngine → expand_dependencies(chunk, chunk_repo, store, max_depth=1)
```

For each top-ranked chunk (when `expand_deps=True`):
- `chunk_repo.get_by_entity(entity_id)` — fetch all chunks for the entity
- `store.get_callees(entity_id)` — walk CALLS relationships one level deep

Returns: `list[ScoredChunk]` with `source` field: `retrieved` (original result) or `dependency` (callee).

SearchEngine returns `List[ScoredChunk]` to RetrievalOrchestrator.

---

> **Note — Semantic fallback**: If semantic retrieval raises an exception,
> RetrievalOrchestrator falls back to lexical search:
> `store.search(query)` + keyword expansion.

---

## Step 13 — [Loop] Synthesize context per entity

For each selected `entity_id` (top `limit_entities` unique entities from the evidence list):

```
RetrievalOrchestrator → ContextSynthesizer:
  service.get_context(
    entity_id, task_type,
    per_entity_max_tokens,
    summarize=(verbosity == 'minimal')
  )
```

Internally:

- **13a** — `synthesize_with_task(entity_id, task_type)` — applies `TASK_TEMPLATES` priority order and per-section boost multipliers for the resolved task type
- **13b** — `_calculate_sufficiency(task_type, content_included, entity, text)` → float `0.0–1.0`

Returns:
```
{
  context_text,
  total_tokens,
  truncated,
  included_entities,
  task_type,
  sufficiency_score
}
```

## Step 14 — Assemble final response

```
RetrievalOrchestrator:
  context_text = '\n---\n'.join(context_parts)
  sufficiency  = avg(sufficiency_scores)
  apply verbosity filter
```

### Verbosity filter

| Level | Fields returned |
|---|---|
| `minimal` | `context_text`, `sufficiency_score`, `total_tokens`, `reduction_summary` |
| `standard` | + `query`, `task_type`, `task_confidence`, `retrieval_mode`, `max_tokens`, `truncated` |
| `verbose` | + `evidence[]` (`rank`, `chunk_id`, `entity_id`, `score`, `source`) |
| `diagnostic` | full dict — all fields + `errors[]` |

---

## Alt A — Return context to caller

**Applies to:** `CLI context` · `REST /api/v1/context/query` · `MCP retrieve_context_for_query`

```
Step 15a:
  KnowCodeService → CLI/REST/MCP:  QueryResponse / ContextResponse
  CLI/REST/MCP → User:             structured context dict
```

---

## Alt B — Ask command: pass to Agent / LLM

**Applies to:** `CLI ask`

### Step 15b — Invoke Agent

```
CLI → Agent:  agent.answer(query)  OR  agent.smart_answer(query, force_llm)
```

### smart\_answer sufficiency check

```
Agent:  check sufficiency_score ≥ threshold  (default 0.8, from AppConfig)
```

- **If sufficient**: `_format_local_answer()` — returns context-only answer; no LLM tokens consumed.
- **If insufficient or `force_llm=True`**: proceed to LLM call below.

### Step 16 — Build prompt

```
Agent:
  get_prompt_template(task_type)  +  context_text  +  question
```

### Step 17 — LLM failover loop

```
[ loop ]  for each model in config.models order (RPM + RPD rate-limit check per model)
```

- **17a** — Google Gemini: `client.models.generate_content(model, prompt)`
- **17b** — OpenAI-compatible (OpenRouter / Mistral): `client.chat.completions.create(model, messages)`
- **17c** — `rate_limiter.record_usage(model.name)` → `~/.knowcode/usage_stats.json`
- **17d** — On `ResourceExhausted` or other error → try next model in list

```
[ end loop ]
```

### Step 18 — Return answer

```
Agent → CLI:  answer text
```

### Step 19 — CLI returns to User

```
CLI → User:  {answer, source=llm|local, task_type, sufficiency_score}
```


---


## MCP Server Workflow


> Textual narration of [`seq_mcp.drawio`](seq_mcp.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode mcp-server`
**Transport:** STDIO / JSON-RPC 2.0
**Clients:** Claude Desktop, VS Code, JetBrains, any MCP-compatible IDE

---

## Participants

| Participant | File | Role |
|---|---|---|
| IDE / Claude Desktop | — | MCP client — sends `tools/call` JSON-RPC requests |
| KnowCodeMCPServer | `mcp/server.py` | MCP server — routes tool calls, formats results |
| KnowCodeService | `service.py` | Central orchestrator — performs all actual work |
| KnowledgeStore | `storage/knowledge_store.py` | In-memory knowledge graph (entity/relationship data) |
| ContextSynthesizer | `analysis/context_synthesizer.py` | Builds task-prioritized context bundles |
| RetrievalOrchestrator | `retrieval/orchestrator.py` | Full hybrid retrieval pipeline (Tool 4 only) |

---

## Startup

### Step 1 — Launch MCP server

```
User → KnowCodeMCPServer:  knowcode mcp-server
```

### Step 2 — Start async runtime

```
KnowCodeMCPServer:  run_server()  →  asyncio.run(run_server_async())
```

### Step 3 — Open STDIO transport

```
KnowCodeMCPServer:  stdio_server(KnowCodeMCPServer)
                    →  STDIO transport  (stdin/stdout pipes)
```

### Step 4 — MCP initialize handshake

```
IDE / Claude Desktop → KnowCodeMCPServer:
  MCP initialize  (JSON-RPC 2.0)
```

### Step 5 — Advertise tools

```
KnowCodeMCPServer → IDE / Claude Desktop:
  tools/list response  →  4 tools with full JSON schemas
```

### Step 6 — Lazy service initialization

```
KnowCodeMCPServer → KnowCodeService:
  KnowCodeService(store_path, strict_config=False)
  [initialized on the first tool call, not at startup]
```

---

## Tool 1 — `search_codebase`

**Signature:** `search_codebase(query: str, limit: int = 10)`

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "search_codebase", arguments: {query, limit}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:  service.search(query)
KnowCodeService   → KnowledgeStore:   knowledge_store.search(query)
```

`knowledge_store.search()` uses substring and token matching on entity `name` and `qualified_name` fields.

### Response

```
KnowledgeStore → IDE:
  [{id, name, qualified_name, kind, file_path, line_start}]  top limit results
```

---

## Tool 2 — `get_entity_context`

**Signature:** `get_entity_context(entity_id: str, task_type: str = "general", max_tokens: int = 2000)`

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "get_entity_context", arguments: {entity_id, task_type, max_tokens}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.get_context(entity_id, task_type, max_tokens)

KnowCodeService → KnowledgeStore:
  entity = store.get_entity(entity_id)  [fallback to store.search() if not found by ID]

KnowCodeService → ContextSynthesizer:
  synthesizer.synthesize_with_task(entity_id, task_type)
  →  applies TASK_TEMPLATES priority order + per-section boost multipliers
```

ContextSynthesizer fetches related nodes from KnowledgeStore:
- `parent` entity
- `callers[]` (entities that call this one)
- `callees[]` (entities this one calls)
- `children[]` (nested entities)

```
ContextSynthesizer:
  _calculate_sufficiency(task_type, content_included, entity, text)  →  float 0.0–1.0
```

### Response

```
ContextSynthesizer → IDE:
  {entity_id, qualified_name, context_text, total_tokens, sufficiency_score, task_type}
```

---

## Tool 3 — `trace_calls`

**Signature:** `trace_calls(entity_id: str, direction: str = "callees", depth: int = 1)`

Valid direction values: `callers` | `callees`. Valid depth range: 1–5.

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "trace_calls", arguments: {entity_id, direction, depth}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.store.trace_calls(entity_id, direction, depth, max_results=50)
```

```
KnowledgeStore:
  BFS traversal on relationship graph
  (CALLS / IMPORTED_BY edges, up to `depth` levels, max_results=50 nodes)
```

### Response

```
KnowledgeStore → IDE:
  [{id, name, qualified_name, kind, file_path, line_start, call_depth}]
```

---

## Tool 4 — `retrieve_context_for_query`

**Signature:**
```
retrieve_context_for_query(
  query: str,
  task_type: str = "auto",
  max_tokens: int = 4000,
  limit_entities: int = 3,
  expand_deps: bool = True,
  verbosity: str = "minimal"
)
```

> **Note on default budgets:** The signature defaults shown above are defined in the python codebase. However, the canonical `docs/mcp-contract.md` recommends clients call the tool with smaller initial request budgets (`max_tokens=1500`, `limit_entities=1`, `expand_deps=false`) to minimize external token consumption.

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {
    name: "retrieve_context_for_query",
    arguments: {query, task_type, max_tokens, limit_entities, expand_deps, verbosity}
  }
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.retrieve_context_for_query(…)

KnowCodeService → RetrievalOrchestrator:
  full hybrid pipeline:
  classify → embed → BM25+FAISS → rerank → expand_dependencies → synthesize
```

> This is the same pipeline described in `seq_query_retrieval.drawio` — steps 4 through 14 apply in full.

```
RetrievalOrchestrator → KnowCodeMCPServer:
  {context_text, sufficiency_score, total_tokens,
   [+ query, task_type, retrieval_mode, evidence[]  per verbosity level]}
```

### Result formatting

```
KnowCodeMCPServer:
  format_result()
  →  MCP content block  {type: "text", text: json.dumps(result)}

KnowCodeMCPServer → IDE:
  tools/call response
```

---

## Error Handling

All tool handler exceptions are caught at the server level. On error the server returns:

```json
{
  "isError": true,
  "content": [{"type": "text", "text": "<error_message>"}]
}
```

No unhandled exception propagates through the STDIO transport.

---

## Tool Summary

| Tool | Arguments | Internal call | Returns |
|---|---|---|---|
| `search_codebase` | `query`, `limit=10` | `knowledge_store.search()` — substring + token match | `[{id, name, qualified_name, kind, file_path, line_start}]` top limit |
| `get_entity_context` | `entity_id`, `task_type=general`, `max_tokens=2000` | `synthesize_with_task()` + `_calculate_sufficiency()` | `{entity_id, qualified_name, context_text, total_tokens, sufficiency_score, task_type}` |
| `trace_calls` | `entity_id`, `direction=callees`, `depth=1` | BFS on relationship graph (max\_results=50) | `[{id, name, qualified_name, kind, file_path, line_start, call_depth}]` |
| `retrieve_context_for_query` | `query`, `task_type=auto`, `max_tokens=4000`, `limit_entities=3`, `expand_deps=true`, `verbosity=minimal` | Full hybrid pipeline (steps 4–14 of seq\_query\_retrieval) | `{context_text, sufficiency_score, total_tokens, …per verbosity}` |


---


## File Watch & Hot-Reload Workflow


> Textual narration of [`seq_file_watch.drawio`](seq_file_watch.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode server --watch`
**Effect:** Every file save triggers an incremental re-index of only that file — no full re-scan needed.

---

## Participants

| Participant | File | Role |
|---|---|---|
| Developer | — | Saves source files in the project |
| CLI | `cli/cli.py` | Parses `server --watch` flag, starts service |
| KnowCodeService | `service.py` | Wires together indexer, monitor, and FastAPI app |
| FileMonitor | `indexing/monitor.py` | watchdog `Observer` — watches filesystem for events |
| IndexingHandler | `indexing/monitor.py` | watchdog event handler — filters and enqueues paths |
| BackgroundIndexer | `indexing/background_indexer.py` | Daemon thread with `Queue` — dequeues and re-indexes |
| Indexer | `indexing/indexer.py` | Parses, chunks, embeds a single file |
| EmbeddingProvider | `llm/embedding.py` | VoyageAI / OpenAI embeddings API |
| KnowledgeStore + VectorStore + ChunkRepo | `storage/` | In-memory stores updated atomically per file |

---

## Startup

### Step 1 — Launch with `--watch`

```
Developer → CLI:  knowcode server --watch
```

### Step 2 — Initialize service

```
CLI → KnowCodeService:  KnowCodeService(store_path, strict_config=True)
```

### Step 3 — Load indexer

```
KnowCodeService:
  service.get_indexer()
  → Indexer(embedding_provider)
  + load(knowcode_index/)  [if existing index found on disk]
```

### Step 4 — Start BackgroundIndexer

```
KnowCodeService → BackgroundIndexer:
  BackgroundIndexer(indexer).start()  →  True
  → one daemon thread started
  + WatchQueue() initialized
  [a second start() returns False rather than adding a consumer]
```

### Step 5 — Start FileMonitor

```
KnowCodeService → FileMonitor:
  FileMonitor(watch_root, bg_indexer).start()
  → watchdog Observer.start()  [uses inotify / FSEvents / kqueue per OS]
```

### Step 6 — Server ready

```
KnowCodeService:  FastAPI + Uvicorn listening on :8000
```

---

## File Change Event

Triggered by OS filesystem notifications forwarded through watchdog.

### Step 7 — Developer saves a file

```
Developer → FileMonitor:  save src/foo.py  (write to filesystem)
```

### Step 8 — Watchdog fires event

```
FileMonitor → IndexingHandler:
  watchdog OS event  →  IndexingHandler.on_modified(FileModifiedEvent)
```

> `on_created` fires the same path: `IndexingHandler.on_created → _queue_index(path)`
> `on_deleted` and `on_moved` map to `queue_removal` and `queue_move` instead.

### Step 9 — Dispatch to handler

```
IndexingHandler:  _queue_index(event.src_path)
```

### Step 10 — Filter

```
IndexingHandler → Scanner:
  scanner.is_indexable(path)
  = extension in SUPPORTED_EXTENSIONS (lowercased)  +  inside the watched root
    +  not ignored
  [the same rule scan() applies in bulk, so watch and build agree]
```

### Step 11 — Enqueue

```
IndexingHandler → BackgroundIndexer:
  bg_indexer.queue_file(file_path)  →  WatchQueue.submit(WatchWork.index(path))
  → coalesced into the one pending item for that canonical identity
```

---

## Background Re-Indexing — `_worker` daemon thread

### Step 12 — Dequeue

```
BackgroundIndexer:  WatchQueue.take()  [blocking; the item stays in flight until committed]
```

### Step 13 — Invoke incremental indexer

```
BackgroundIndexer → Indexer:  indexer.replace_file(work.path)
  then indexer.delete_file(src) for each dropped source (a rename's old identity)
  [on a retryable failure: back off and retry, up to max_attempts;
   otherwise record it in failures() — the previous generation stays searchable]
```

### Step 14 — Parse file

```
Indexer:
  parse file with appropriate language parser (Tree-sitter)
  → ParseResult{entities[], relationships[]}
```

### Step 15 — Chunk entities

```
Indexer:
  Chunker.process_parse_result()
  → CodeChunks[]  {id, entity_id, content, tokens[], metadata}
  (module header chunk + import block chunk + entity chunks with BM25 tokens)
```

### Step 16 — Embed chunks

```
Indexer → EmbeddingProvider:
  embedding_provider.embed(chunk_texts[])
  → VoyageAI / OpenAI Embeddings API call
```

```
EmbeddingProvider → Indexer:
  vectors  (list[list[float]], L2-normalized)
```

### Step 17 — Update ChunkRepository

```
Indexer → ChunkRepository:
  remove old chunks for entity
  add new chunks
```

### Step 18 — Update VectorStore

```
Indexer → VectorStore:
  remove old vectors for entity
  add new vectors
  → rebuild FAISS IndexFlatIP
```

### Step 19 — Update KnowledgeStore (No-op in Incremental Indexing)

```
Note: Indexer.index_file() does NOT update the KnowledgeStore.
The static knowledge graph is kept unchanged on disk (knowcode_knowledge.json)
until a full 'knowcode analyze' is executed or a reload is triggered.
```

### Step 20 — Persist to disk

```
Indexer:
  indexer.save(index_path)
  atomic write:
    → chunks.json         (all CodeChunk objects)
    → vectors.index       (FAISS binary index)
    → vectors.json        (metadata: schema version, dimension, model name)
```

### Step 21 — Re-index complete

```
BackgroundIndexer:  ✓ re-index complete
                    next API request sees fresh data  (no server restart needed)
```

---

## Manual Reload — `POST /api/v1/reload`

This is a separate mechanism that clears the in-memory **knowledge graph** cache (not the semantic index).

### Step 22 — POST reload

```
Developer → KnowCodeService:  POST /api/v1/reload
```

### Step 23 — Clear cache

```
KnowCodeService:
  service.reload()  →  _store = None  [clears in-memory KnowledgeStore cache]
```

### Step 24 — Lazy reload on next access

```
KnowCodeService → KnowledgeStore:
  next access to service.store
  → KnowledgeStore.load(store_path)  reads knowcode_knowledge.json from disk
```

```
KnowCodeService → Developer:  {status: "reloaded"}
```

---

## Contrast: Incremental vs Full Reload

| Mechanism | Scope | Triggered by |
|---|---|---|
| `FileMonitor → BackgroundIndexer` (steps 7–21) | **Incremental**: re-indexes only the single changed file; updates `ChunkRepo`, `VectorStore`, and `KnowledgeStore` in memory | File save detected by watchdog |
| `POST /api/v1/reload` (steps 22–24) | **Cache clear only**: discards in-memory `KnowledgeStore`; reloads from `knowcode_knowledge.json` | Manual API call |
| `knowcode analyze` (separate command) | **Full rebuild**: GraphBuilder re-scans all files, rebuilds knowledge graph, then Indexer re-scans for semantic index | CLI command |


---


## Agent Gateway Workflow


> Textual narration of [`seq_agent_gateway.drawio`](seq_agent_gateway.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Gateway Sequence (Historical Reference)**
**Note:** The Agent Gateway has been extracted from this repository. The sequence below describes its behavior at extraction time.
**Request entry:** `POST /api/v1/chat`

---

---

## Participants

| Participant | File | Role |
|---|---|---|
| User / IDE | — | Sends chat requests to the Gateway |
| Gateway FastAPI `:8081` | `app.py` | HTTP server — validates requests, delegates to orchestrator |
| AgentOrchestrator | `orchestrator.py` | Agentic tool-use loop (max 4 rounds) |
| ToolSelector | `tool_selector.py` | Keyword heuristics — selects tool subset from user message |
| OpenAPIToolRegistry | `openapi_tools.py` | Caches OpenAI-compatible tool schemas derived from KnowCode OpenAPI spec |
| LiteLLMClient | `litellm_client.py` | Sends chat completion requests to LiteLLM proxy |
| LiteLLM Proxy `:4000` | external | Normalizes to upstream LLMs (Gemini, Mistral, …) |
| KnowCodeClient | `knowcode_client.py` | Dispatches tool calls to KnowCode REST API |
| KnowCode REST API `:8000` | `src/knowcode/api/api.py` | The main KnowCode service API |

---

## Startup — `local_up.sh`

### Step 1 — Start dependencies

```
local_up.sh:
  → start KnowCode REST API on :8000
  → start LiteLLM proxy on :4000
```

### Step 2 — Load settings

```
Gateway:  GatewaySettings.from_env()
```

Settings loaded (frozen dataclass, all from environment variables):

| Setting | Default |
|---|---|
| `knowcode_api_base_url` | — (required) |
| `litellm_base_url` | — (required) |
| `default_model` | — (required) |
| `max_tool_rounds` | `4` |
| `tool_timeout_seconds` | `30` |
| `openapi_cache_ttl_seconds` | `300` |

### Step 3 — Fetch OpenAPI spec

```
Gateway → KnowCode REST API:
  GET  {knowcode_api_base_url}/openapi.json
```

```
KnowCode REST API → OpenAPIToolRegistry:  OpenAPI spec JSON
```

### Step 4 — Translate to tool schemas

```
OpenAPIToolRegistry:
  OpenAPIToolTranslator.translate(openapi_spec)
  →  OpenAI-compatible tool schema list  (cached for 300 s)
```

### Step 5 — Gateway ready

```
Gateway:  listening on :8081
```

---

## Agentic Request — `POST /api/v1/chat`

### Step 6 — Receive chat request

```
User / IDE → Gateway:
  POST /api/v1/chat
  ChatRequest{
    message,
    conversation[],
    model,
    tags,
    tool_names,
    temperature
  }
```

### Step 7 — Delegate to orchestrator

```
Gateway → AgentOrchestrator:  orchestrator.run(chat_request)
```

### Step 8 — Select tools

```
AgentOrchestrator → ToolSelector:
  _pick_tool_names(request)  →  select_tool_names(message)
```

Keyword heuristics (not ML):

| Keyword pattern | Tool selected |
|---|---|
| `explain`, `what is`, `describe` | `get_context` |
| `find`, `search`, `where` | `search` |
| `trace`, `who calls`, `callers` | `trace_calls` |
| (default) | all four tools |

Returns: subset of `{query_context, search, get_context, trace_calls}`.

### Step 9 — Fetch tool schemas

```
AgentOrchestrator → OpenAPIToolRegistry:
  get tool schemas for selected tools
```

Returns: list of OpenAI-compatible tool schema dicts.

---

## Tool-Use Loop — up to `max_tool_rounds=4` iterations

### Step 10 — LLM completion with tools

```
AgentOrchestrator → LiteLLMClient:
  litellm_client.create_chat_completion(
    messages, tools=tool_schemas, model, temperature
  )
```

### Step 11 — Forward to LiteLLM proxy

```
LiteLLMClient → LiteLLM Proxy:
  POST  http://litellm_base_url/chat/completions
```

### Step 12 — Upstream LLM call

```
LiteLLM Proxy:  proxy → upstream LLM  (Gemini / Mistral / …)
```

### Step 13 — Receive completion response

```
LiteLLM Proxy → LiteLLMClient:
  ChatCompletion{
    choices[0].finish_reason,
    choices[0].message.tool_calls[]
  }
```

### Step 14 — Extract tool call

```
LiteLLMClient → AgentOrchestrator:
  _first_choice(response)  →  tool_call{id, name, arguments}
```

---

### [if `finish_reason == "tool_calls"`] — Execute tool call (timeout = 30 s)

### Step 15 — Dispatch to KnowCodeClient

```
AgentOrchestrator → KnowCodeClient:
  _execute_tool_call(tool_call)  →  knowcode_client.execute_tool(name, args)
```

### Step 16 — KnowCodeClient dispatches to REST API

KnowCodeClient maps tool names to REST endpoints:

| Tool name | HTTP call |
|---|---|
| `query_context` | `POST /api/v1/context/query  {query, limit, task_type}` |
| `search` | `GET  /api/v1/search?q=...` |
| `get_context` | `GET  /api/v1/context?target=...&task_type=...` |
| `trace_calls` | `GET  /api/v1/trace_calls/{entity_id}?direction=...&depth=...` |

### Step 17 — API result returned

```
KnowCode REST API → KnowCodeClient:  result JSON
```

### Step 18 — Record execution

```
KnowCodeClient → AgentOrchestrator:
  ToolExecutionRecord{
    tool_name,
    tool_call_id,
    arguments,
    success,
    latency_ms
  }
```

### Step 19 — Append result and continue loop

```
AgentOrchestrator:
  append tool_result to messages[]
  → continue loop
```

---

**Loop exits when:** `finish_reason == "stop"` OR `max_tool_rounds` reached.

---

## Final Response

### Step 19 — Build ChatResponse

```
AgentOrchestrator → Gateway:
  ChatResponse{
    answer,
    model,
    usage{},
    response_cost,
    finish_reason,
    selected_tools[],
    tool_executions[]
  }
```

### Step 20 — Return to caller

```
Gateway → User / IDE:  ChatResponse
```

---

## Smoke E2E — `scripts/smoke_e2e.py`

Used in CI post-deploy validation or run manually.

### Step 21 — Health check

```
smoke_e2e.py → Gateway:  GET /health
Gateway:  assert {status: "ok"}
```

### Step 22 — Tools check

```
smoke_e2e.py → Gateway:  GET /api/v1/tools
Gateway:  assert count ≥ 1 tool available
```

### Step 23 — Chat round-trip

```
smoke_e2e.py → Gateway:
  POST /api/v1/chat
  {message: "Use query_context and get_context to find search logic..."}
```

### Step 24 — Validate response

```
smoke_e2e.py:
  assert answer != ''
  assert len(tool_executions) ≥ SmokeConfig.min_tool_calls
  [optional: filter by specific tool_name]
```
