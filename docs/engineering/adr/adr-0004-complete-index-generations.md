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


### Amended by ADR 0009 (2026-08-29)

The layout above shows `vectors/...` inside a generation directory, and
validation above covers chunk/vector membership. Both are superseded by
[ADR 0009](adr-0009-derived-vector-plane.md): the ANN index is a derived cache
rebuilt from the durable embeddings in `chunks.db`, so it is not published,
digested, copied forward, or retained. Everything else in this ADR stands,
including staging, whole-generation validation, pointer-last publication,
leases, retention, and rollback.
