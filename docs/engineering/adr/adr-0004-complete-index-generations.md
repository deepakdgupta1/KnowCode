# ADR 0004: Complete index generations

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

---


### Decision

The artifact root contains immutable generation directories plus one current
pointer:

```text
artifact-root/
  generations/
    <generation-id>/
      knowledge.db
      chunks.db
      vectors/...
      manifest.json
  current.json
```

A full or incremental build creates a unique same-filesystem staging
generation without holding the publication lock. It flushes and validates the
knowledge store, chunks, vectors, and manifest together. Validation covers
contract/schema versions, generation ID, checksums, embedding configuration,
counts, unique chunk IDs, and chunk/vector membership.

After validation, publication takes the generation write lock, finalizes the
staged directory, and atomically replaces `current.json` last. Readers acquire
one generation lease and use it for sparse, dense, graph, and context work.
Failure before pointer replacement leaves the old generation searchable.
Superseded generations are retired only after reader leases end, and at least
one last-known-good generation is retained.

Replace-style JSON metadata uses a same-directory temporary file, flush, file
`fsync`, `os.replace`, and parent-directory `fsync` where supported. Data is
published before its manifest, and the generation pointer is published last.

### Recovery and rollback

Startup validates the pointed generation. An invalid pointer or generation may
fall back only to a completely valid retained generation; individual artifact
files are never mixed. Orphan staging directories are ignored and later
cleaned. Rollback moves the pointer to a compatible complete generation or
rebuilds; it never downgrades one artifact in place.


### Amendment (2026-08-29): the vector plane is not a published artifact

The layout above shows `vectors/...` inside a generation directory. It is no
longer written there.

ADR 0003 makes the little-endian float32 BLOB in `chunks.db` the durable
embedding record and requires that vector rebuilding read committed chunk rows.
That makes the on-disk ANN index derived data. Publishing it stored every
vector twice, digested the second copy, copied it into every incremental build,
and retained it per generation. Measured on this repository, that was 32.31 MB
of a 113.18 MB generation.

A published generation now contains `knowledge.db`, `chunks.db`,
`index_manifest.json`, `manifest.json`, and `preflight_report.json`. The vector
store is held in memory and filled by
`Indexer.rebuild_vector_plane` from the durable chunk rows when a generation is
loaded. On 6,874 vectors at 1024 dimensions the rebuild takes 0.3 seconds and
reproduces the persisted plane exactly, verified as identical ids and identical
scores over the top 25.

Two validation rules move with it.

A full generation no longer has to record a native vector artifact. That rule
asserted the presence of a cache.

Chunk and vector membership is now checked by counting rows in `chunks.db` that
carry a non-null embedding and comparing that against the vector count the
manifest records. The previous rule compared two numbers inside the same
manifest, so it could not see a generation whose chunk rows had lost their
embeddings. This is the rule ADR 0003 already required and the code did not
enforce.

Generations written before this change still load. They carry a native vector
artifact, so their persisted plane stays authoritative and no rebuild runs.

Above roughly 100,000 vectors an in-memory plane stops being the right default.
The intended shape there is a disk-backed ANN cache outside `generations/`,
keyed by the chunk-id digest the manifest already computes, so retention never
copies it. That is not implemented.
