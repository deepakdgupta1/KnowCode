# ADR 0003: Durable embedding representation

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

---


### Decision

SQLite chunks persist embeddings as little-endian float32 BLOBs accompanied by
an explicit dimension. Insert and load validate byte length, configured
dimension, and finite values. The generation manifest records provider, model,
dimension, normalization, and embedding-format version.

An embedding may be null only for a generation explicitly configured without
dense retrieval. A dense generation cannot be published if any searchable
chunk lacks a valid embedding. Vector rebuilding reads committed chunk rows;
it never depends on transient `CodeChunk.embedding` values.

### Compatibility

The current chunk schema has no embedding column. It cannot be losslessly
migrated without calling an embedding provider, so it fails closed with a full
semantic-rebuild instruction. Provider calls are never hidden inside schema
migration or startup.

