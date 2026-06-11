# Research Report v3: Lossless Storage and Retrieval Optimization for KnowCode

**Status:** Draft  
**Date:** 2026-06-11  
**Builds on:** `docs/research/storage_optimization_2026.md` and `docs/research/storage_optimization_2026_v2.md`

## 1. Executive Summary

This v3 strategy focuses only on optimizations that improve storage and performance without weakening KnowCode's lossless retrieval objective.

The central design principle is:

> Store source text once, index it with reversible metadata and exact candidate-generation structures, and verify every returned result against current source bytes.

The strongest lossless architecture is therefore:

1. Keep the source tree as the only full source-code payload.
2. Replace duplicated JSON source/chunk content with compact source descriptors.
3. Encode paths, symbols, chunks, terms, and graph edges through deterministic integer codebooks.
4. Build exact text, path, symbol, token, and gram indexes with compressed postings.
5. Use full-precision vectors only as an optional ranking or navigation layer, never as the correctness layer.
6. Store large postings and vector slabs in mmap/segment files so startup and process RAM stay low.
7. Validate every returned snippet by source hash or byte-range hash.

The result is not a "few MB total artifact" for 50M+ LOC. That target is incompatible with instantaneous exact global retrieval. The few-MB target can apply to the control manifest; the exact index and any full-precision vector layer must scale with corpus size.

## 2. Losslessness Contract

### 2.1 Lossless operations

KnowCode can claim 100% correctness only for mechanically verifiable operations over fresh indexed files:

- exact file lookup,
- exact path lookup,
- exact symbol lookup for successfully indexed symbols,
- exact substring search,
- regex search after source verification,
- source snippet retrieval by byte range after hash validation.

### 2.2 Non-lossless operations

The following must not be used as the correctness layer:

- semantic embedding search,
- approximate nearest-neighbor search,
- HNSW or IVF pruning unless exhaustive coverage is used,
- cross-encoder reranking,
- LLM query expansion,
- static call graph traversal in languages/features where completeness cannot be proven.

These mechanisms may improve ranking or exploration, but they cannot be used to guarantee complete retrieval.

### 2.3 Verification rule

Every returned source result must pass this rule:

```text
descriptor -> file_id, byte_start, byte_end, expected_hash
read current source bytes
verify file hash or byte-range hash
return snippet only if valid
```

If validation fails, KnowCode must reindex the file or refuse to claim exactness.

## 3. Reference Architecture

```text
                         +----------------------+
                         |      Query Planner   |
                         +----------+-----------+
                                    |
          +-------------------------+--------------------------+
          |                         |                          |
          v                         v                          v
+-------------------+     +---------------------+     +---------------------+
| Exact Index Plane |     | Source Descriptor   |     | Structural Graph    |
| path/symbol/text  |     | Plane               |     | Plane               |
| token/gram regex  |     | byte ranges/hashes  |     | confidence edges    |
+---------+---------+     +----------+----------+     +----------+----------+
          |                          |                           |
          +--------------------------+---------------------------+
                                     |
                                     v
                         +----------------------+
                         | Source Resolver      |
                         | current file bytes   |
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | Verified Context     |
                         +----------------------+
```

Supporting planes:

```text
Codec Plane:
  deterministic IDs, string tables, symbol tables, path tables, edge codes

Freshness Plane:
  file hashes, dirty overlay, generation IDs, tombstones, atomic segment publish

Optional Vector Plane:
  full-precision fp32 vectors for ranking/navigation only, never for exactness
```

## 4. Storage Plan by Layer

| Layer | Main contents | Storage posture | Lossless role |
|---|---|---|---|
| Control manifest | file IDs, hashes, schema, generations | few MB to tens of MB | freshness and routing |
| Codec tables | strings, paths, symbols, terms, kinds | compact, reversible | decode IDs to names |
| Source descriptors | file ID, byte ranges, line ranges, hashes | compact | map results to source |
| Exact postings | paths, symbols, tokens, grams | proportional to source | exact candidate generation |
| Structural graph | entities and confidence-labeled edges | proportional to symbols/edges | verified context expansion |
| Optional vector slabs | fp32 vectors and IDs | GB to tens of GB | ranking/navigation only |
| Freshness overlay | dirty files, tombstones, generation log | small | stale-result prevention |

The source tree remains the only full source-code store.

## 5. Deterministic Codec and Codebook Layer

The "codex" optimization should be implemented as deterministic reversible encoding, not learned compression.

### 5.1 Encode repeated identities

Represent paths, symbols, chunks, terms, relationship kinds, languages, and metadata keys as integer IDs.

```text
String table:
  1 -> "src"
  2 -> "knowcode"
  3 -> "retrieval"
  4 -> "search_engine.py"
  5 -> "SearchEngine"
  6 -> "search_scored"

Path table:
  file_id=421 -> [1, 2, 3, 4]

Symbol table:
  symbol_id=98231 -> file_id=421, parent_id=5, name_id=6, kind=method

Source descriptor:
  descriptor_id=77 -> file_id=421, byte_start=1290, byte_end=3180, symbol_id=98231
```

Internal search should operate on encoded IDs. Decode only the final result set.

### 5.2 Codec requirements

- Deterministic.
- Reversible.
- Stable within an index generation.
- Corruption-detecting through checksums.
- Independent of source reconstruction.

Never use a lossy learned codec for source text, symbol identity, byte ranges, or graph identity when the system is making exactness claims.

### 5.3 Benefit profile

High benefit:

- file paths,
- qualified names,
- repeated path components,
- symbol names,
- relationship kinds,
- token dictionaries,
- postings metadata,
- graph edges,
- chunk/source descriptor IDs.

Low benefit:

- raw fp32 vector arrays,
- already-compressed postings payloads,
- unique source text.

## 6. Source Descriptor Model

Persist descriptors instead of chunk bodies.

```text
descriptor_id
  -> file_id
  -> entity_id or symbol_id
  -> byte_start
  -> byte_end
  -> line_start
  -> line_end
  -> source_hash or range_hash
  -> descriptor_kind
```

Descriptor kinds:

- module header,
- imports block,
- symbol declaration,
- symbol body,
- AST subchunk,
- docstring/comment block,
- test usage block.

Descriptors are cheap and reversible. Source text is materialized only after retrieval and validation.

## 7. Exact Retrieval Plane

The exact retrieval plane is the lossless search layer.

### 7.1 Required indexes

Build encoded indexes for:

- `path_index`: path component, full path, suffix, basename.
- `symbol_index`: symbol name, qualified name, kind, language, file.
- `term_index`: code tokens and identifier splits.
- `gram_index`: trigrams or sparse grams for substring and regex prefiltering.
- `facet_index`: language, generated/vendor/test, package, directory.
- `range_index`: file ID to line and byte offsets.

### 7.2 Postings format

Use compressed postings instead of JSON token arrays.

```text
term_id -> block_list
block -> doc_id_delta_stream, optional tf, optional positions, skip metadata
```

Recommended compression:

- delta-encoded document IDs,
- block bitpacking,
- skip pointers,
- optional positions only where needed,
- zstd only for cold metadata blocks, not hot postings that require frequent random access.

### 7.3 Substring and regex search

For substring and regex support:

1. Extract required grams from the query or regex.
2. Intersect gram postings to get candidates.
3. Read current source bytes for candidates.
4. Run literal or regex verification against the actual source.

The gram index is a prefilter, not the proof. Source verification is the proof.

### 7.4 Query flow

```text
query
  -> classify exact intents
  -> convert terms/grams/symbols/paths to encoded IDs
  -> intersect postings and filters
  -> materialize candidate source ranges
  -> verify exact match
  -> rank verified results
  -> decode final IDs
```

This flow preserves losslessness because every returned result is checked against current source.

## 8. Full-Precision Vector Plane

Full-precision vectors are allowed only if their role is clear:

> They improve semantic ranking and navigation, but they do not provide the lossless guarantee.

### 8.1 Storage math

```text
raw_vector_bytes = vector_count * dimension * 4
```

Examples:

| Vector count | 1024D fp32 | 1536D fp32 |
|---:|---:|---:|
| 300k | 1.2 GB | 1.8 GB |
| 700k | 2.9 GB | 4.3 GB |
| 1M | 4.1 GB | 6.1 GB |
| 2M | 8.2 GB | 12.3 GB |
| 5M | 20.5 GB | 30.7 GB |
| 10M | 41.0 GB | 61.4 GB |

This is raw vector storage only. ID maps, vector routers, metadata, exact indexes, snapshots, and replication are additional.

### 8.2 Lossless-safe vector usage

Allowed uses:

- rank already source-verified exact candidates,
- navigate from a conceptual query to likely areas,
- prioritize which exact searches or graph neighborhoods to inspect,
- cluster related symbols for exploration,
- provide fallback suggestions labelled as semantic candidates.

Disallowed uses for exact mode:

- claiming complete retrieval from vector nearest neighbors,
- replacing exact text/symbol indexes,
- using approximate vector pruning as the only candidate source for exact answers,
- using fp16/PQ/SQ when the result is advertised as full-precision or lossless.

### 8.3 Storage placement

Do not store vectors in JSON. Do not require all vectors in Python heap.

Lossless-compatible placement options:

1. **Mmap fp32 slab store**
   - Store contiguous `float32` arrays in segment files.
   - Keep vector ID to descriptor ID mapping in encoded tables.
   - Let the OS page cache handle hot pages.
   - Prefetch only exact-filtered or shard-selected ranges.

2. **Segmented fp32 store**
   - Partition vectors by repository, package, language, or descriptor class.
   - Load or mmap only the segments selected by exact filters or query planning.
   - Preserve raw fp32 values.

3. **Database-backed fp32 store**
   - Acceptable only when the store preserves raw full-precision vectors and can be used for exact-filtered exhaustive scoring or ranking outside the lossless path.
   - The database is a storage and operational boundary, not a correctness boundary.

### 8.4 Exact vector search caveat

Flat exhaustive vector search is exact nearest-neighbor search, but it must compare against all candidate vectors unless exact filters shrink the candidate set.

```text
exact filters
  -> candidate descriptor IDs
  -> corresponding vector segments
  -> exhaustive fp32 scoring inside that candidate set
```

This is lossless for nearest-neighbor within the filtered candidate set. It is not a substitute for exact source search.

### 8.5 Adaptive vector volume

For a fixed codebase, vector count is determined by embedding policy.

A lossless-first policy should embed only units that improve semantic ranking while keeping exact retrieval independent:

- file/module summaries,
- public or central symbols,
- large or complex symbols,
- AST-aware subchunks for oversized symbols,
- docstring/signature views only for high-value entities.

Avoid embedding:

- generated code by default,
- vendored code by default,
- trivial getters/setters unless query logs show value,
- overlapping windows everywhere,
- repeated contextual headers inside every chunk.

## 9. Adaptive Embedding Planner

The `EmbeddingPlanner` selects vector units under a storage budget. It does not affect exact retrieval coverage.

### 9.1 Features to measure

Per file:

- LOC and byte size,
- language,
- parser confidence,
- generated/vendor/test classification,
- symbol count,
- average and max symbol length,
- comment/docstring density,
- import density,
- churn and recency.

Per symbol:

- kind,
- byte length,
- structural complexity,
- fan-in and fan-out,
- public/private/exported state,
- docstring presence,
- test references,
- change frequency,
- name ambiguity.

### 9.2 Utility scoring

```text
utility =
  public_api_weight
  + graph_centrality_weight
  + complexity_weight
  + query_likelihood_weight
  + exact_search_gap_weight
  - generated_code_penalty
  - trivial_symbol_penalty
  - vector_storage_cost
  - update_churn_cost
```

Select highest-utility vector units until the vector budget is reached.

### 9.3 Planning bands for 50M LOC

| Policy | Approx vectors | 1024D fp32 raw vectors | 1536D fp32 raw vectors |
|---|---:|---:|---:|
| File summaries only | 100k-300k | 0.4-1.2 GB | 0.6-1.8 GB |
| Lean symbol-focused | 300k-700k | 1.2-2.9 GB | 1.8-4.3 GB |
| Balanced symbol + selective chunks | 1M-2M | 4.1-8.2 GB | 6.1-12.3 GB |
| Rich multi-view | 3M-6M | 12.3-24.6 GB | 18.4-36.9 GB |

The vector budget changes semantic ranking quality, not exact retrieval correctness.

## 10. Structural Graph Plane

The graph improves context, but it must carry confidence labels.

### 10.1 Entity records

Store:

- entity ID,
- file ID,
- symbol ID,
- kind,
- byte range,
- line range,
- signature hash,
- source hash,
- parser version,
- parser confidence.

### 10.2 Edge records

```text
edge_id -> source_entity_id, target_entity_id, relationship_kind_id, confidence_id, provenance_id
```

Confidence levels:

- `exact_ast`,
- `resolved_import`,
- `type_checker`,
- `ctags_symbol`,
- `heuristic_name_match`,
- `unresolved`.

Only high-confidence edges should support strong claims. Heuristic edges should be used for navigation and ranking, not completeness.

### 10.3 Graph expansion

Graph expansion may add:

- parent classes,
- sibling methods,
- imports,
- tests,
- direct callees/callers,
- related config/routes.

Every expanded source descriptor still requires source verification before return.

## 11. Freshness and Transactions

Freshness is part of the losslessness guarantee.

### 11.1 Manifest contents

Store:

- schema version,
- index generation ID,
- root fingerprint,
- file ID,
- relative path ID,
- size,
- mtime,
- content hash,
- language,
- generated/vendor/test flag,
- last indexed generation per layer.

### 11.2 Update flow

On modify:

1. Mark file dirty immediately.
2. Recompute hash.
3. Rebuild affected descriptors, exact postings, graph records, and optional vectors.
4. Write new segments.
5. Atomically publish a new generation.
6. Retire old segments after active readers finish.

On delete:

1. Tombstone file ID.
2. Hide postings through deletion bitmaps.
3. Hide graph edges through generation filters.
4. Compact later.

On rename:

1. Preserve blob identity if content hash matches.
2. Update path index.
3. Rebind descriptors atomically.

## 12. Query Planner

The planner should prefer lossless routes first.

### 12.1 Query intents

Classify into:

- `exact_text`,
- `identifier`,
- `path`,
- `symbol`,
- `regex`,
- `stack_trace`,
- `config_key`,
- `structural`,
- `semantic`.

### 12.2 Layer priority

Default order:

1. exact path/symbol/text indexes,
2. metadata filters,
3. source verification,
4. graph expansion with confidence labels,
5. optional vector ranking/navigation,
6. final source verification after expansion.

For conceptual queries, vectors may help choose likely areas, but exactness is claimed only for verified source returned from exact descriptors.

### 12.3 Provenance-preserving scoring

Use separate score channels:

```text
final_score =
  exact_match_score
  + symbol_score
  + path_score
  + graph_score
  + optional_semantic_score
  + freshness_score
```

The response should retain provenance for each result.

## 13. Deployment Modes

### 13.1 Lite Mode

Goal: minimal storage.

- Manifest.
- Codec tables.
- Small path/symbol catalog.
- Source scan for global exact search.
- No global instantaneous guarantee.

### 13.2 Exact Mode

Goal: lossless instantaneous local retrieval.

- Manifest.
- Codec tables.
- Source descriptors.
- Exact postings.
- Freshness overlay.
- Confidence-labeled graph.

This is the primary mode for the stated objective.

### 13.3 Exact+Vector Mode

Goal: lossless exact retrieval plus better semantic ranking.

- Exact Mode.
- Adaptive fp32 vector slabs.
- Vectors are explicitly ranking/navigation only.
- Every returned source snippet still comes from verified descriptors.

## 14. Storage Budget Framework

Total artifact storage:

```text
total =
  manifest
  + codec_tables
  + source_descriptors
  + exact_postings
  + structural_graph
  + optional_fp32_vectors
  + vector_id_maps
  + tombstones_and_snapshots
```

Only the manifest and codec/control plane should be expected to fit in a few MB for very large repositories.

Planning ranges for 50M LOC:

| Component | Lean | Balanced | Rich |
|---|---:|---:|---:|
| Manifest + codec | 10-100 MB | 20-200 MB | 50-300 MB |
| Source descriptors | 50 MB-1 GB | 100 MB-2 GB | 500 MB-5 GB |
| Exact index | hundreds of MB | 1-5 GB | 2-10+ GB |
| Structural graph | 100 MB-2 GB | 500 MB-5 GB | 1-10 GB |
| Optional fp32 vectors | 0-4 GB | 4-12 GB | 12-40+ GB |

Exact numbers require measurement on representative repositories.

## 15. Evaluation and Guardrails

### 15.1 Hard correctness gates

- Exact substring recall on fresh files: 100%.
- Exact symbol lookup for indexed symbols: 100%.
- Stale snippet return rate: 0.
- Delete/rename correctness: 100% in deterministic tests.
- Source verification failure handling: no silent stale answers.
- Codec round-trip correctness: 100%.

### 15.2 Performance metrics

- startup latency,
- warm exact query p50/p95/p99,
- cold exact query p50/p95/p99,
- source verification latency,
- incremental update latency,
- compaction time,
- mmap page-fault behavior.

### 15.3 Storage metrics

- source bytes,
- unique source bytes,
- descriptor bytes,
- exact index bytes,
- graph bytes,
- codec bytes,
- optional vector bytes,
- bytes per LOC,
- bytes per symbol,
- bytes per descriptor.

### 15.4 Optional vector metrics

Only for Exact+Vector Mode:

- Recall@10/20 for semantic queries,
- MRR,
- Precision@1,
- vector bytes per successful semantic retrieval,
- rank delta against exact-only baselines where applicable.

These metrics must not be mixed with exact correctness guarantees.

## 16. Implementation Roadmap

### Phase 0: Measurement

1. Extend `scripts/measure_storage.py` to estimate:
   - unique source bytes,
   - source descriptor count,
   - symbols,
   - graph edges,
   - exact postings size,
   - optional vector counts by policy.
2. Add a `storage-plan` command that prints projected artifact sizes.
3. Add exact retrieval golden tests for text, symbol, regex, stale file, delete, and rename behavior.

### Phase 1: Codec and Source Descriptors

1. Introduce file IDs, symbol IDs, path IDs, term IDs, and string tables.
2. Persist descriptors instead of source or chunk content.
3. Remove source from persisted entities by default.
4. Add file and range hashes.
5. Decode only final results.

### Phase 2: Exact Index

1. Prototype exact text/path/symbol search with compressed postings or an existing exact-search substrate.
2. Add gram prefilter plus source verification.
3. Add path, symbol, and facet filters.
4. Add delete/rename/tombstone correctness.
5. Benchmark warm and cold query latency.

### Phase 3: Freshness and Transactions

1. Add dirty-file overlay.
2. Add atomic index generations.
3. Add tombstones and compaction.
4. Add reader-safe segment retirement.
5. Refuse exactness when validation fails.

### Phase 4: Graph Hardening

1. Add edge confidence and provenance.
2. Preserve unresolved references.
3. Add reverse edges and task-aware context expansion.
4. Prevent graph traversal from making unproved completeness claims.

### Phase 5: Optional Full-Precision Vector Layer

Only after exact retrieval is correct:

1. Add adaptive vector planning.
2. Store fp32 vectors in mmap/segment files.
3. Map vector IDs to verified source descriptors.
4. Use vectors only for ranking/navigation.
5. Evaluate semantic quality separately from exact correctness.

## 17. Decisions to Lock

Recommended decisions:

- Exact source retrieval is the correctness layer.
- Source text is stored once: in the source tree.
- Descriptor, path, symbol, term, and graph metadata are encoded reversibly.
- Exact postings are the search substrate.
- All returned snippets are hash-validated.
- Optional vectors are full precision and never the lossless proof.
- Approximate vector indexes, rerankers, and LLM expansion cannot support exactness claims.

Decisions to avoid:

- Do not claim a few-MB total artifact for instantaneous exact retrieval over 50M LOC.
- Do not use lossy codecs for source identity or source reconstruction.
- Do not embed every fixed-size window by default.
- Do not store repeated contextual headers inside every chunk.
- Do not call fp16 or vector quantization lossless.
- Do not call cross-encoder reranking verification.
- Do not trust live source reads without freshness checks.

## 18. Reference Notes

The v3 strategy is aligned with established exact-search and storage patterns:

- GitHub Blackbird uses code-specific ngram indexes, lazy postings, sharding, compaction, and source post-verification for large-scale code search: <https://github.blog/engineering/architecture-optimization/the-technology-behind-githubs-new-code-search/>
- Zoekt is a trigram code search engine with substring, regexp, boolean query, and symbol-aware ranking support: <https://github.com/sourcegraph/zoekt>
- Russ Cox explains regex search through trigram indexes and candidate verification: <https://swtch.com/~rsc/regexp/regexp4.html>
- SQLite FTS5 supports trigram tokenization plus external-content and contentless storage modes: <https://sqlite.org/fts5.html>
- Tantivy provides Lucene-inspired segment-based full-text indexing: <https://docs.rs/tantivy/latest/tantivy/>
- Faiss documents flat vector indexes as `ntotal * code_size` storage and distinguishes exact flat search from pruned approximate families: <https://github.com/facebookresearch/faiss/wiki/Faiss-indexes>
- cAST shows AST-aware code chunking can improve code retrieval compared with fixed-size chunking, which supports adaptive descriptor/vector planning: <https://arxiv.org/abs/2506.15655>
- Zstandard is useful for cold metadata blocks, dictionaries, and compact segment storage: <https://facebook.github.io/zstd/>
