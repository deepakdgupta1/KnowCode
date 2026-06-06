# Sequence Diagram — Indexing / Analysis Workflow

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
- On file save: `IndexingHandler.on_modified()` or `on_created()` → `_handle_change(path)` → extension filter → `bg_indexer.queue_file(path)`
- `BackgroundIndexer._worker()` (daemon thread): dequeues paths, calls `indexer.index_file(path)`
- `index_file(path)` re-runs steps 14–17 for the single changed file only (incremental, not full re-scan)
- After re-index: the next API request automatically sees fresh data (no server restart needed)

`POST /api/v1/reload` clears the in-memory `KnowledgeStore` cache; on next access it re-reads `knowcode_knowledge.json` from disk.
