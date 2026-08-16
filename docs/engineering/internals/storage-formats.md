# Internals: Storage Formats

On-disk artifacts, their schemas, and the fail-closed versioning policy.
Decision records: [ADR 3](../adr/adr-0003-durable-embedding-representation.md),
[ADR 7](../adr/adr-0007-protocol-and-artifact-evolution-inventory.md),
[ADR 8](../adr/adr-0008-persistence-format-and-token-economics.md).

## Artifact sets

| Artifact | File(s) | Schema | Notes |
|---|---|---|---|
| Knowledge store (JSON) | `knowcode_knowledge.json` | 2 | Entities + relationships; readable by the legacy exporter but cannot mix into a current generation |
| Knowledge store (SQLite) | `knowledge.db` | generation-scoped | Canonical in generations |
| Chunk repository | `chunks.db` (SQLite) | 2 | Chunks + durable embeddings |
| Vector store | `vectors.lancedb` + `vectors.json` (LanceDB, default) | 2 | `vector_backend: lancedb` |
| Vector store | `vectors.index` + `vectors.json` (FAISS/NumPy) | 3 | `vector_backend: faiss` |
| Generation manifest | `manifest.json` | 3 | Contract version, generation ID, checksums, embedding config, knowledge/chunk/vector counts |
| Preflight report | `preflight_report.json` | — | Written beside generation artifacts; loaded by doctor/MCP/CLI |
| Telemetry | `knowcode_telemetry.jsonl` | versioned | Store root, never inside `knowcode_index/` (see [telemetry](../../user/telemetry.md)) |

## SQLite chunk schema (v2)

- Embeddings persist as **little-endian float32 BLOBs with an explicit
  dimension**; insert and load validate byte length, configured dimension,
  and finite values ([ADR 3](../adr/adr-0003-durable-embedding-representation.md)).
- A dense generation cannot publish if any searchable chunk lacks a valid
  embedding; null embeddings are legal only in explicitly non-dense
  generations.
- Connections: one per execution context; WAL, `busy_timeout`, FK
  enforcement; a repository-level writer coordinator serializes write
  transactions ([ADR 2](../adr/adr-0002-sqlite-connection-and-transaction-ownership.md)).
- File updates go through transactional `replace_file(file_id, chunks)` —
  prepare/commit, returning previous and committed IDs with generation
  metadata.

## Vector backends

Both implement `VectorStoreProtocol` (`protocols.py`): `add`, `upsert`
(exact-ID idempotent), `flush` (write-visibility boundary), `search`,
`get_embedding`, `save`, `load`, `clear`, `remove`, `count`, `dimension`,
`close`. Typed errors in `knowcode.errors`: `VectorDimensionError`,
`VectorArtifactVersionError`, `VectorContractError`. `hasattr` is not
capability negotiation.

Locking matrix: mutations (`add/upsert/remove/clear/flush/save/load`)
serialize under the mutation lock; reads (`search/get_embedding/count`)
are snapshot-safe and observe one generation.

### LanceDB (default), metadata schema 2

- **Chunk IDs are data, not filter syntax.** LanceDB 0.33 accepts only
  string predicates, so every row carries `key` = SHA-256 hex digest of
  its chunk ID; all predicates are built by central helpers from that
  digest (asserted `^[0-9a-f]{64}$`), and exactness is re-verified in
  Python against the returned row. A hostile repository ID (e.g.
  `x' OR true --`) cannot reach the filter grammar — this is why IDs are
  digest-keyed rather than escaped.
- Mutable write buffer keyed by chunk ID + pending-delete set; **reads
  flush first**, `flush` applies deletes before inserts (a replacement
  never leaves two rows); deletes batched at 128 digests.
- `vectors.json` envelope (`schema_version`, `backend`, `dimension`,
  `count`, `generation`) is fully validated before adoption: version,
  backend match, dimension parity, artifact presence both ways, `key`
  column presence, duplicate IDs, digest collisions, declared count.
- A path with no table loads as an empty index (`lancedb.connect` creates
  the directory — its presence alone is not evidence).

### FAISS/NumPy, metadata schema 3

- ID-aware native index: exact-ID assignment/upsert/removal;
  native-tombstone parity (`index.ntotal == count()`), removed IDs never
  consume top-k slots, duplicate adds collapse to one live row.
- Envelope records backend, dimension, contract version, generation, and
  native/live count parity; inconsistencies reject with
  `VectorArtifactVersionError`. Envelope-level checksums are deliberately
  absent — artifact digests live in the generation manifest (see
  [indexing & generations](indexing-generations.md)), cross-checking all
  artifacts of one generation.

## Versioning & migration policy (fail-closed)

Legacy artifacts — chunk schema 1, LanceDB metadata 1, FAISS metadata 2,
manifest schema 2, pre-generation SQLite knowledge stores — **fail closed**
with one actionable message naming the artifact and the rebuild command
(`knowcode build`). Nothing is migrated in memory, no loader stamps the
current version onto an unverified payload, and provider calls are never
hidden inside migration or startup. Rationale per artifact class:
[ADR 7](../adr/adr-0007-protocol-and-artifact-evolution-inventory.md).

## Storage economics

Source text is stored in up to three places (knowledge store, chunk
repository, original files); measured duplication, token flow, and the
phased deduplication proposal are analyzed in
[ADR 8](../adr/adr-0008-persistence-format-and-token-economics.md)
(status: Proposed; reproduce numbers with `scripts/measure_storage.py`).
