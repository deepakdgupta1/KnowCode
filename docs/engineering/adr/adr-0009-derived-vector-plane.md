# ADR 0009: The vector index is a derived cache, not a published artifact

**Status:** Accepted

**Date:** 2026-08-29

**Amends:** [ADR 0004](adr-0004-complete-index-generations.md) (generation
layout and two validation rules)

**Builds on:** [ADR 0003](adr-0003-durable-embedding-representation.md)

**Origin:** `docs/research/storage_optimization_2026_v4.md` Phase D1

---

### Context

ADR 0003 makes the little-endian float32 BLOB in `chunks.db` the durable
embedding record and requires that vector rebuilding read committed chunk rows
rather than transient in-memory values. ADR 0004's generation layout
nonetheless placed a `vectors/` directory inside every generation, and
publication digested it, validated it, copied it into every incremental build,
and retained it per generation.

That stored every embedding twice. Measured on this repository with 6,874
chunks at 1024 dimensions, the durable BLOBs were 26.85 MB and the published
ANN index was 32.31 MB of a 113.18 MB generation, 28.5%. The index also
accumulated 292 fragments and 293 superseded version manifests, because the
store was pointed directly at the generation directory and its `save` copied
that directory wholesale.

### Decision

The ANN index is a derived cache. It is not published, digested, validated,
copied forward, or retained.

A published generation contains `knowledge.db`, `chunks.db`,
`index_manifest.json`, `manifest.json`, and `preflight_report.json`.

The vector store is constructed in memory. `create_vector_store` takes no
directory, so no store can write inside a bundle. This matters more than
removing the save call: `lancedb.connect` created the table inside the
generation as a side effect of construction, so leaving the path in place would
have kept publishing the artifact no matter what `save` did.

`Indexer.rebuild_vector_plane` fills the store from the durable chunk rows,
streamed by `SqliteChunkRepository.iter_embeddings`. It clears first, so it
converges: running it twice is running it once. `Indexer.load` rebuilds when the
generation carries no native vector artifact, and loads the persisted plane when
it does.

Two validation rules change.

A full generation no longer has to record a native vector artifact. That rule
asserted the presence of a cache.

Chunk and vector membership is checked by counting rows in `chunks.db` that
carry a non-null embedding and comparing that against the vector count the
manifest records. The rule it replaces compared two numbers inside the same
manifest, so it could not see a generation whose chunk rows had lost their
embeddings. The new rule is what ADR 0003 already required and the code did not
enforce.

### Consequences

One generation on this repository falls from 113.18 MB to 78.87 MB. 32.31 MB of
that is this decision; the rest is free-page variance between two SQLite builds.
Two retained generations fall from about 226 MB to about 158 MB, because the
plane is no longer copied per generation.

Retrieval is unchanged. Verified on 6,874 vectors with the plane as the only
variable: 10 of 10 end-to-end searches identical, top-25 raw vector ids
identical, maximum score delta 0.000e+00.

A rebuild costs 0.30 seconds for 6,874 vectors at 1024 dimensions, paid once per
process open. It reads no network and calls no embedding provider.

A process now holds the plane in RAM for its lifetime, about 27 MB here. That is
the tradeoff, and it is the reason for the limit below.

### Compatibility

Generations written before this decision still load. They carry a native vector
artifact, so their persisted plane stays authoritative and no rebuild runs. No
schema version moves. Such a generation keeps its vector directory until it ages
out of retention.

### Limits

An in-memory plane is the wrong default above roughly 100,000 vectors, where the
resident cost stops being negligible. The intended shape there is a disk-backed
int8 ANN cache outside `generations/`, keyed by the chunk-id digest the manifest
already computes, so retention never copies it, plus an exhaustive fp32 scan
below the threshold where no index is warranted at all. Neither is implemented.
Both are tracked in the [backlog](../backlog.md).
