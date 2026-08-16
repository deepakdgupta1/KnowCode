# Internals: Indexing & Generations

How builds, incremental updates, and watch mode produce and swap index
artifacts. Component map: [architecture](../architecture.md); decision
records: [ADR 4](../adr/adr-0004-complete-index-generations.md),
[ADR 3](../adr/adr-0003-durable-embedding-representation.md).

## The build pipeline

`knowcode build` / `knowcode index` (entry: `indexing/indexer.py`):

1. **Scan** (`Scanner`) — file discovery honoring the root `.gitignore`,
   filtered to supported extensions.
2. **Parse** — per-language parsers produce `ParseResult`s (entities,
   locations, relationships).
3. **Graph build** (`GraphBuilder`) — entities/relationships assembled;
   unresolved references remain visible as `unresolved::…` targets
   (scored by preflight, never silently dropped).
4. **Chunk** (`chunker.py`) — entity-aligned chunks (signature +
   docstring + source; 1000 chars / 100 overlap) plus module-header and
   imports chunks; MD5 content hash, mtime, and `has_docstring` recorded
   as rerank signals.
5. **Embed** (`llm/embedding.py`) — provider selection VoyageAI →
   OpenAI-compatible → deterministic `DummyEmbeddingProvider` (SHA-derived
   pseudo-embeddings: search still works, BM25-dominated).
6. **Stage & publish** (`generation_writer.py`) — see below.

## Generations

The artifact root holds immutable generation directories plus one pointer:

```text
artifact-root/
  generations/<generation-id>/{knowledge.db, chunks.db, vectors/, manifest.json}
  current.json
```

- A build creates a unique **staging** directory on the same filesystem,
  without holding the publication lock.
- The staged generation is **validated as a whole** before publication:
  contract/schema versions, generation ID, checksums, embedding
  configuration, counts, unique chunk IDs, chunk↔vector membership.
- Publication takes the generation write lock, finalizes the staged
  directory, and atomically replaces `current.json` **last**
  (`os.replace` + directory fsync — data before manifest, pointer last).
- **Readers acquire one generation lease** and use it for sparse, dense,
  graph, and context work; a live server hot-swaps to a new generation
  when its leases end. At least one last-known-good generation is
  retained; superseded generations are retired only after leases drain.
- Failure anywhere before pointer replacement leaves the previous
  generation current and the CLI exits non-zero — no half-built state is
  ever observable.

Startup validates the pointed generation; an invalid pointer may fall back
only to a completely valid retained generation. Orphan staging directories
are ignored and cleaned later.

## Durable embedding cache

Chunks unchanged since the last build (matched by `content_hash`) reuse
their stored embeddings — no provider call. Vector rebuilds read committed
chunk rows, never transient in-process embeddings. Dense generations
cannot publish if any searchable chunk lacks a valid embedding; a null
embedding is legal only for an explicitly non-dense generation.

## Watch mode

`knowcode server --watch` wires:

- `FileMonitor` (watchdog `IndexingHandler`) — filesystem events, with
  ignore-status checks (events for ignored files are dropped).
- `WatchQueue` — debounces and orders events into file-update transactions.
- `file_updates.py` — **prepare/commit transactions**: each file update is
  prepared (parse/chunk/embed) and committed atomically against the
  generation; a prepare failure is retryable (`FileUpdatePreparationError`)
  and never zeroes a file's existing chunks.
- `BackgroundIndexer` — worker thread executing queued transactions;
  joins cleanly on shutdown (`join()` wired in the app lifespan).
- `ServiceWatchWriter` — publishes new generations as updates accumulate;
  the server hot-swaps via the lease mechanism above.

Deleted/renamed files invalidate and remove their old chunks. Freshness
metadata (`service.get_freshness_metadata`) compares artifact state against
newest source change; reasons include `store_stale_source_changed` and
`index_stale_source_changed`; the stale flag propagates into retrieval
responses (advisory — flagged, never blocked).
