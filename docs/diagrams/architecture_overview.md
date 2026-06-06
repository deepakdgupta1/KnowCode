# KnowCode — System Architecture

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
| `export` | Export the knowledge graph as Markdown documentation |
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

### Agent Gateway (FastAPI `:8081`)

A separately deployable microservice (in `apps/agent-gateway/`) that proxies to the KnowCode REST API and wraps it in an LLM-driven tool-use loop. Its own endpoints:

- `GET  /health` — gateway liveness
- `GET  /ready` — checks KnowCode + LiteLLM connectivity
- `GET  /api/v1/config` — current gateway configuration
- `GET  /api/v1/tools` — list of available tools (from OpenAPI translation)
- `POST /api/v1/chat` — submit a message; returns answer + tool execution records

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
- `IndexingHandler.on_modified(event)` + `on_created(event)` → `_handle_change(path)` → extension filter → `bg_indexer.queue_file(path)`
- `start()` / `stop()`

**`BackgroundIndexer` (`indexing/background_indexer.py`)**
- Daemon thread + `threading.Queue`
- `queue_file(path)` — enqueues a file path for re-indexing
- `_worker()` — blocking dequeue loop, calls `indexer.index_file(path)` for each entry
- `start()` / `stop()`

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

## Agent Gateway (Separate Microservice)

Located in `apps/agent-gateway/`. Can be moved to an independent repository without code changes.

### `GatewaySettings` (`settings.py`)

Frozen dataclass loaded from environment variables via `from_env()`:

| Setting | Default |
|---|---|
| `knowcode_api_base_url` | `http://127.0.0.1:8000` |
| `litellm_base_url` | `http://127.0.0.1:4000` |
| `litellm_api_key` | `sk-local-proxy` |
| `default_model` | `gemini/gemini-3-flash-preview` |
| `max_tool_rounds` | `4` |
| `tool_timeout_seconds` | `30.0` |
| `openapi_cache_ttl_seconds` | `300` |
| `allowed_tool_names` | `{query_context, search, get_context, trace_calls}` |

### `AgentOrchestrator` (`orchestrator.py`)

- `run(ChatRequest)` → `ChatResponse` — the main agentic loop:
  1. `_pick_tool_names(request)` → `select_tool_names(message)` (keyword heuristics)
  2. Fetch tool schemas from `OpenAPIToolRegistry`
  3. Loop ≤ `max_tool_rounds`:
     - `LiteLLMClient.create_chat_completion(messages, tools)`
     - `_first_choice(response)` extracts `tool_call`
     - `_execute_tool_call(tool_call, timeout)` → `KnowCodeClient.execute_tool()`
     - Append tool result to messages, record `ToolExecutionRecord`
  4. Build and return `ChatResponse`
- `list_tools()` → available tool names
- `readiness()` → checks KnowCode + LiteLLM health

### `tool_selector.py` (Module)

- `select_tool_names(message)` — keyword heuristics on the user message text
- Returns a subset of `allowed_tool_names` based on detected intent

### `LiteLLMClient` (`litellm_client.py`)

- `create_chat_completion(messages, tools, model, temperature)` → sends to LiteLLM proxy `:4000`
- `check_health()` — pings LiteLLM
- `_extract_response_cost(response)` — extracts cost metadata

### `KnowCodeClient` (`knowcode_client.py`)

- `execute_tool(tool_name, args)` — dispatches to KnowCode REST API:
  - `query_context` → `POST /api/v1/context/query`
  - `search` → `GET  /api/v1/search?q=...`
  - `get_context` → `GET  /api/v1/context?target=...`
  - `trace_calls` → `GET  /api/v1/trace_calls/{entity_id}?direction=...&depth=...`
- `check_health()` — pings KnowCode `/api/v1/health`

### `OpenAPIToolRegistry` + `OpenAPIToolTranslator` (`openapi_tools.py`)

- `fetch_openapi_spec(url)` → fetches `/openapi.json` from KnowCode
- `OpenAPIToolTranslator` converts OpenAPI operation objects into OpenAI-compatible tool schema dicts
- Results cached for `openapi_cache_ttl_seconds = 300` seconds

### LiteLLM Proxy (`:4000`)

- Configured via `litellm.config.yaml`
- Accepts OpenAI-compatible requests and proxies to configured upstream LLMs (Google Gemini, others)
- Manages rate-limit passthrough

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
