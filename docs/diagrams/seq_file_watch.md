# Sequence Diagram — File Watch / Hot-Reload Workflow

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
  BackgroundIndexer(indexer).start()
  → daemon thread started
  + Queue() initialized
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

> `on_created` fires the same path: `IndexingHandler.on_created → _handle_change(path)`

### Step 9 — Dispatch to handler

```
IndexingHandler:  _handle_change(event.src_path)
```

### Step 10 — Filter

```
IndexingHandler:
  filter: file extension in SUPPORTED_EXTENSIONS  +  not gitignored
  [path is silently dropped if filter fails]
```

### Step 11 — Enqueue

```
IndexingHandler → BackgroundIndexer:
  bg_indexer.queue_file(file_path)  →  Queue.put(file_path)
```

---

## Background Re-Indexing — `_worker` daemon thread

### Step 12 — Dequeue

```
BackgroundIndexer:  Queue.get(file_path)  [blocking dequeue]
```

### Step 13 — Invoke incremental indexer

```
BackgroundIndexer → Indexer:  indexer.index_file(file_path)
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

### Step 19 — Update KnowledgeStore

```
Indexer → KnowledgeStore:
  update entities + relationships for the changed file
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
