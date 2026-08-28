# Research Report v4: Measured First-Build Storage Footprint

**Status:** Draft
**Date:** 2026-08-28
**Builds on:** `docs/archive/storage_optimization_2026.md` (v1),
`storage_optimization_2026_v2.md`, `storage_optimization_2026_v3.md`
**Closes:** v3 Phase 0 ("Measurement"), which was never executed against the
current SQLite/LanceDB artifact layout

---

## 1. Executive Summary

v1 through v3 proposed architectures. None of them measured the artifacts
that `knowcode build` actually writes today, because the measurement tool
(`scripts/measure_storage.py`) still targeted the retired JSON layout
(`knowcode_knowledge.json`, `chunks.json`, `vectors.index`) and failed on
startup. Every storage number in v3 is therefore a planning range, not an
observation.

This report measures a real first build. On this repository — 221 Python
files, 2.08 MB of Python, 8.72 MB of all tracked files — one published
generation is **87.9 MB**, a **42x amplification over the Python source**
and **10x over the entire tracked tree**. Because the default retention is
two generations, a second build takes the directory to **~176 MB**.

The footprint is not dominated by irreducible index structure. It is
dominated by four specific, fixable causes:

| Cause | Cost | Nature |
|---|---:|---|
| Every embedding stored twice at full fp32 width | 52.5 MB | one copy is a derived cache |
| Vector width applied to chunks with no retrievable text | 8.2 MB | no embedding-selection policy |
| Absolute paths used as primary and foreign keys | see §6 | encoding choice |
| Derived data persisted as if durable | see §7 | `tokens_text`, `source_code`, duplicated hashes |

The last two overlap — integer-keying an edge table also removes the path
strings inside it — so they are quoted per artifact rather than summed.
Authoritative totals are the measured end-states in §9.

**63.5% of the first-build footprint is recoverable** (87.9 MB to 32.1 MB)
without weakening any correctness guarantee in v3's losslessness contract,
and without a single change to retrieval semantics beyond which chunks are
offered as semantic candidates. **54.5%** (to 40.0 MB) is recoverable with
zero change to what is retrievable at all.

Two tools now make these numbers reproducible:

```bash
python scripts/measure_storage.py     # where the bytes are
python scripts/storage_simulate.py    # what each change actually recovers
```

`storage_simulate.py` applies every candidate to a throwaway copy of the
published artifacts and measures the resulting file. Nothing below is
modeled unless labeled as such.

---

## 2. Method

Measurements come from the generation currently published in this
repository, `20260828T100515254661Z-fa1be334`, built with `voyage-code-3`
at 1024 dimensions, `max_chunk_size=1000`, `overlap=100`.

Four layers are measured:

1. **Artifacts** — `stat()` bytes for every file in the generation.
2. **Tables** — page usage per table and per index via `dbstat`.
3. **Columns** — payload bytes per column, which is what separates derived
   and duplicated data from irreducible payload.
4. **Vector plane** — vector count, width, and the ratio of vector bytes to
   the chunk text each vector describes.

Candidate savings are measured by transformation, not estimated: copy the
artifact, apply the change, `VACUUM`, `stat()`. Where a number is modeled
rather than transformed, it is marked **(modeled)**.

Note on units: the report uses file bytes. Actual disk usage is higher
because the vector index is spread across 273 fragment files averaging 95 KB,
each rounding up to a filesystem block; `du` reports 30.6 MB where `stat()`
reports 28.4 MB.

---

## 3. Where the Bytes Are

### 3.1 Artifacts

| Artifact | Bytes | Share |
|---|---:|---:|
| `chunks.db` | 39.93 MB | 45.4% |
| `vectors.lancedb` | 28.43 MB | 32.4% |
| `knowledge.db` | 19.48 MB | 22.2% |
| manifests (`manifest.json`, `index_manifest.json`, `vectors.json`, `preflight_report.json`, `current.json`) | < 4 KB | ~0% |
| **Total** | **87.90 MB** | |

The control plane is already tiny — v3's "few MB for the manifest" target is
comfortably met. The entire problem is in the three data artifacts.

### 3.2 Column payload

`chunks.db` — 6,166 rows:

| Column | Bytes | Note |
|---|---:|---|
| `embedding` | 24.09 MB | fp32 BLOB, 1024d, L2-normalized |
| `content` | 3.47 MB | duplicates the source tree |
| `tokens_text` | 2.97 MB | `" ".join(tokenize_code(content))` — fully derived |
| `metadata_json` | 0.81 MB | includes a `content_hash` duplicate |
| `chunk_id` | 0.59 MB | absolute path prefix |
| `entity_id` | 0.57 MB | absolute path prefix |
| `file_path` | 0.40 MB | absolute path |

`knowledge.db` — 4,561 entities, 20,900 relationships:

| Column | Bytes | Note |
|---|---:|---|
| `entities.source_code` | 2.45 MB | duplicates the source tree |
| `entities.metadata_json` | 1.43 MB | duplicates `content_hash` |
| `relationships.target_id` | 2.35 MB | absolute-path TEXT key |
| `relationships.source_id` | 2.02 MB | absolute-path TEXT key |
| `entities.entity_id` | 0.44 MB | absolute path prefix |
| `entities.content_hash` | 0.28 MB | 64-char hex |

### 3.3 Tables and indexes

| Table or index | Bytes |
|---|---:|
| `chunks` | 36.39 MB |
| `entities` | 6.96 MB |
| `relationships` | 4.97 MB |
| `idx_relationships_target_kind` | 3.46 MB |
| `idx_relationships_source_kind` | 3.02 MB |
| `chunks_fts_data` | 1.20 MB |

The relationship plane costs **11.45 MB to hold 20,900 edges — 574 bytes per
edge**. An edge carries two entity references and a kind. The cost comes from
storing those references as absolute-path TEXT in the table and then
repeating the same strings in both covering indexes.

Note the FTS index itself is well built: `content='chunks'` external-content
mode means `chunks_fts_data` is only 1.20 MB and stores no duplicate text.
The waste is one layer up, in `tokens_text`.

---

## 4. Root Cause 1: Vectors Stored Twice (52.5 MB)

`chunks.db` holds 24.09 MB of fp32 embeddings. `vectors.lancedb` holds
25.30 MB of vector data plus 3.13 MB of bookkeeping. These are the same
6,166 vectors.

This is not accidental. ADR 0003 makes the SQLite BLOB the durable record,
and `indexer.py::_recover_vectors_from_chunks` rebuilds the vector store
from committed chunk rows:

```python
chunk = self.chunk_repo.get(chunk_id)
if chunk is None or chunk.embedding is None:
    logger.error("Cannot rebuild vectors for %s: no durable embedding", chunk_id)
    return False
self.vector_store.upsert(chunk_id, chunk.embedding)
```

**The architectural fact this establishes: `vectors.lancedb` is a derived
cache, not a durable artifact.** 28.43 MB — 32% of the first build — is
fully reconstructible from bytes already on disk. Nothing requires it to be
published, retained per generation, or copied forward.

### 4.1 The bookkeeping is pure waste

`lancedb_vector_store.py` calls `self._table.add(rows)` per batch and never
compacts or cleans versions. Neither `compact_files` nor
`cleanup_old_versions` appears anywhere in the module. Result:

- 273 data fragments averaging 95 KB
- 274 superseded version manifests totaling 3.10 MB
- 273 transaction records totaling 0.03 MB

3.13 MB of that is recoverable by calling the compaction API that already
exists, with no design change at all. The fragmentation costs more than the
file bytes suggest: 546 of these files are small enough that block rounding
inflates them, which is why `du` reports 30.6 MB against 28.4 MB of actual
content.

### 4.2 Width is a policy question, not a correctness one

v3 §8 is explicit that vectors are a ranking and navigation layer, never the
correctness layer, and equally explicit that quantization must not be called
lossless. Both hold. But that framing means the *derived ANN index* may be
narrower than the *durable record*.

Measured on this repository's own vectors (6,166 × 1024, L2-normalized,
300 real chunk vectors as queries, exhaustive fp32 top-10 as ground truth):

| ANN width | Size | recall@10 |
|---|---:|---:|
| fp32 | 24.09 MB | 1.0000 |
| fp16 | 12.04 MB | 0.9997 |
| int8 (per-vector scalar) | 6.02 MB | 0.9950 |
| binary (sign) | 0.75 MB | 0.8110 |
| binary + fp32 rescore of top-100 | 0.75 MB + durable | 0.9983 |

fp32 vectors are also nearly incompressible — zlib takes 24.09 MB to
22.31 MB, a 7% gain — confirming v3 §5.3's "low benefit" classification. The
lever on the vector plane is count and width, never compression.

### 4.3 Recommendation

Keep the durable fp32 BLOB exactly as ADR 0003 specifies. Change the derived
plane:

1. Call `compact_files()` and `cleanup_old_versions()` before publish. **(3.13 MB, no design change.)**
2. Stop publishing the vector index as a generation artifact. Build it on
   first semantic query, or lazily at load. **(25.30 MB.)**
3. Below roughly 100k vectors, skip the ANN index entirely — an exhaustive
   fp32 dot product over 6,166 × 1024 is a few million multiply-adds and
   completes in well under a millisecond. An ANN index at this scale buys
   nothing and costs 28 MB.
4. Where an on-disk ANN index is warranted for large repositories, build it
   int8. 4x smaller at 0.995 recall@10, with the fp32 record still available
   for exact rescoring.

---

## 5. Root Cause 2: No Embedding-Selection Policy (8.2 MB)

Every chunk gets a 4,096-byte vector regardless of how much text it holds.

| Content size | Chunks | Content | Vectors | Amplification |
|---|---:|---:|---:|---:|
| < 100 B | 1,046 | 38 KB | 4.09 MB | **110x** |
| 100–250 B | 821 | 144 KB | 3.21 MB | 23x |
| 250–500 B | 1,196 | 428 KB | 4.67 MB | 11x |
| 500–1000 B | 1,308 | 908 KB | 5.11 MB | 6x |
| >= 1000 B | 1,795 | 2,030 KB | 7.01 MB | 4x |

**1,046 chunks (17% of the corpus) hold 38 KB of text between them and cost
4.09 MB of vectors.** Sampling what they are:

```
[ 8B] KnowCode
[20B] YAML (custom parser)
[29B] logger = get_logger(__name__)
[31B] 4.4 Structural Fusion Retrieval
[35B] Phase 3: Structural Graph Hardening
[36B] """Rust parser using Tree-sitter."""
[47B] _CAMEL_SPLIT_RE = re.compile(r"([a-z])([A-Z])")
[53B] from knowcode.utils.token_counter import TokenCounter
```

Markdown headings detached from their bodies, single-line module constants,
bare docstrings, individual import lines. An 8-byte heading gets a
4,096-byte embedding. These are not semantic retrieval candidates; they are
already reachable through the exact symbol and path planes.

By chunk type:

| Type | Chunks | Content | Vectors | Amplification |
|---|---:|---:|---:|---:|
| (untyped) | 5,697 | 3,119 KB | 22.25 MB | 7x |
| `module_header` | 254 | 373 KB | 0.99 MB | 3x |
| `imports` | 215 | 57 KB | 0.84 MB | **15x** |

`imports` blocks are a distinct case: their content is a list of module
paths, and the same information is already stored as import edges in
`relationships`. Embedding them buys nothing the graph does not already
answer better.

Also measured: 118 chunks are byte-identical to another chunk, costing
0.46 MB of duplicate vectors. `metadata_json` already carries a
`content_hash` for every chunk, so deduplication needs no new machinery.

**The vector plane is 7x the size of the text it describes.** Projected
against content thresholds:

| Minimum content | Chunks kept | Vector plane |
|---|---:|---:|
| (none) | 6,166 | 24.09 MB |
| >= 60 B | 5,294 | 20.68 MB |
| >= 100 B | 5,120 | 20.00 MB |
| >= 250 B | 4,299 | 16.79 MB |
| >= 400 B | 3,546 | 13.85 MB |

**Recommendation.** Implement the `EmbeddingPlanner` from v3 §9, starting
with its cheapest possible form: a minimum-content threshold, skip
`imports` blocks, and deduplicate by content hash. Measured effect on
`chunks.db` at a 250 B threshold plus import skipping: **8.19 MB (20.5%)**.
Merge headings into their section bodies rather than emitting them as
standalone chunks — this recovers retrieval quality that the current
chunking loses, and shrinks the artifact at the same time.

This is the one recommendation in this report that changes retrieval
behavior. It should ship behind the evaluation harness so the recall effect
is measured rather than assumed. The prior is favorable: the pruned chunks
contain almost no retrievable text.

---

## 6. Root Cause 3: Absolute Paths as Keys

Entity ids are absolute paths:

```
/Users/deepg/Desktop/KnowCode/.agent/rules/analysis-integrity.md::analysis-integrity
```

v1 flagged this in June 2026 — both the ~15% size cost and the portability
break. It is still present, and it is worse than v1 estimated because the
ids are also foreign keys in `relationships` and are then repeated in two
covering indexes.

Two independent problems:

**The repo prefix.** 30 bytes repeated across roughly 27,620 stored path
fields plus every index that covers them. Measured saving from repo-relative
paths alone: **2.04 MB in `chunks.db`, 4.46 MB in `knowledge.db`**.

**TEXT keys in the edge table.** Measured saving from integer entity ids
plus a relationship-kind codebook: **6.80 MB, 34.9% of `knowledge.db`** —
the single largest lossless win available. This is v3 §5's codec plane
applied to the one place it pays most.

Beyond bytes, both changes fix real defects. Absolute paths make a
generation non-portable across machines, checkouts, and containers — the
artifact cannot be cached in CI or shared between developers. Integer keys
also make graph traversal cheaper: comparing 4-byte integers instead of
100-byte strings.

---

## 7. Root Cause 4: Derived Data Persisted as Durable

Four cases, in descending order of clarity:

**`chunks.tokens_text` — 2.97 MB.** Written as
`" ".join(tokenize_code(content))` and read back as `tokens_text.split()`.
Purely derived. It exists because FTS5 external-content mode needs a
readable column, and `tokenize_code` performs identifier splitting that
`unicode61` cannot reproduce from raw content. The fix is a contentless FTS5
table (`content=''`): the term index survives, the text column does not.
This forgoes `snippet()` and `highlight()`, which the codebase does not use,
and forgoes rebuilding the FTS index without re-tokenizing — acceptable,
since re-tokenizing is deterministic and cheap. Measured: **5.64 MB (14.1%)**,
more than the raw column because dropping it also recovers page overhead.

**`entities.source_code` — 2.45 MB.** Read in exactly one place,
`service.py:1657`, and every field needed to resolve it from disk is already
stored: `file_path`, `line_start`, `line_end`, `content_hash`. Measured:
**4.77 MB (24.5%)**. This is v3's source-descriptor model, and v3 §11 argues
it is also *more correct* — resolving from disk with hash verification
cannot return stale source, whereas a persisted copy silently can.

**Duplicated `content_hash` — 2.09 MB.** Present as a first-class column and
again inside `metadata_json`:

```json
{"behavior": {...}, "confidence": {...},
 "content_hash": "4e1336e7dabe17aef171a291300755b6aeec3640045de7ddb7bb81190e51ac10"}
```

**`content_hash` stored as 64-char hex — 1.76 MB.** `unhex()` yields the
identical digest in 32 bytes. No collision-resistance change, half the bytes.

**`chunks.content` — 3.47 MB.** A duplicate of the source tree, but unlike
the others it has live consumers: `reranker.py:154` and `:217`. Replacing it
with byte-range descriptors requires a source resolver, so it belongs to a
later phase. Measured if done: **6.39 MB (16.0%)**.

---

## 8. Root Cause 5: Retention Multiplies Everything

`DEFAULT_RETAINED_GENERATIONS = 2`. Every byte above is paid twice from the
second build onward: 87.9 MB becomes ~176 MB, which is what this repository
currently holds.

Retention exists for a real reason — atomic generation publish with
reader-safe retirement (ADR 0004). But it currently retains *complete*
generations including the derived vector index, and it copies forward data
that has not changed.

Two independent improvements:

1. Once the vector index is derived rather than published (§4), retention
   costs 32% less immediately, with no policy change.
2. Retention need not duplicate unchanged content. Content-addressed
   chunk and entity storage shared across generations, with generations
   holding only manifests and deltas, makes the marginal cost of a retained
   generation proportional to what actually changed. This is a larger change
   and belongs after the items in §9.

---

## 9. Recommended Program

Ordered by return per unit of risk. Every figure is measured by
`scripts/storage_simulate.py` against this repository's published generation.

**Individual savings are measured in isolation and are not additive.**
Integer-keying the edge table also removes the absolute paths inside it, so
the two overlap heavily: applied separately they recover 6.80 MB and 4.46 MB
of `knowledge.db`, but every Phase B and C change applied together recovers
12.08 MB, not 19.88 MB. The authoritative figures are the measured
per-artifact combinations and end-states in bold.

### Phase A — free (no persisted contract changes)

| Change | Saving |
|---|---:|
| `compact_files()` + `cleanup_old_versions()` before publish | 3.13 MB |
| `VACUUM` before publish | 2.03 MB |

**87.9 MB to 82.7 MB.** Both call APIs that already exist.

### Phase B — lossless encoding (same information, fewer bytes)

| Change | Measured in isolation |
|---|---:|
| Integer-keyed relationships + kind codebook | 6.80 MB |
| Repo-relative paths in `knowledge.db` | 4.46 MB |
| Repo-relative paths in `chunks.db` | 2.04 MB |
| Drop duplicated `content_hash` from `metadata_json` | 2.09 MB + 1.10 MB |
| `content_hash` as 32-byte BLOB | 1.76 MB |

Also fixes generation portability across machines, checkouts, and CI.

### Phase C — stop persisting derived data

| Change | Measured in isolation |
|---|---:|
| Vector index becomes a rebuildable cache | 28.43 MB |
| `tokens_text` to contentless FTS5 | 5.64 MB |
| `entities.source_code` resolved from disk | 4.77 MB |

**Measured end-state, all of A + B + C applied together:**

| Artifact | Before | After |
|---|---:|---:|
| `chunks.db` | 39.93 MB | 32.56 MB |
| `knowledge.db` | 19.48 MB | 7.39 MB |
| `vectors.lancedb` | 28.43 MB | 0 (rebuilt on demand) |
| **Total** | **87.90 MB** | **39.96 MB — 54.5% smaller** |

Nothing here changes what is retrievable.

If an on-disk ANN index is required rather than rebuilt, an int8 plane adds
6.02 MB for a total of 45.98 MB, at a measured recall@10 of 0.995.

### Phase D — embedding-selection policy (gate on evals)

| Change | Measured in isolation |
|---|---:|
| Minimum content threshold (250 B) + skip `imports` | 8.19 MB |
| Deduplicate vectors by content hash | 0.46 MB |
| Merge headings into section bodies | (a quality fix too) |

**Measured end-state, A + B + C + D: 32.05 MB — 63.5% smaller**, and 15x the
Python source rather than 42x. `chunks.db` alone falls from 39.93 MB to
24.66 MB.

This is the only phase that changes retrieval behavior and the only one that
should be gated on the evaluation harness.

### Phase E — source descriptors (v3 Phase 1 proper)

Replace `chunks.content` with verified byte-range descriptors and a source
resolver. Worth **6.39 MB** here, but the real argument is correctness:
v3 §2.3's verification rule cannot be honored while snippets are served from
a persisted copy that may be stale.

### Phase F — cross-generation content addressing

Share unchanged chunk and entity storage across retained generations so the
marginal cost of retention is proportional to the delta (§8).

---

## 10. What This Changes in the v3 Doctrine

v3's architecture holds. Three corrections to its framing:

**The problem is not index structure; it is duplication and unfiltered
policy.** v3 planned for exact postings, gram indexes, and codec tables that
do not exist yet. Meanwhile 60% of the current footprint is one artifact
stored twice plus vectors attached to text with no retrievable content.
Phases A–D above deliver a 63.5% reduction without building any of v3's new
index planes.

**Vector count, not vector width, is the lever — and neither is
compression.** v3 §9's `EmbeddingPlanner` is the single highest-value
unbuilt component, and its cheapest form (a content threshold) captures most
of the benefit.

**Derived-versus-durable is the organizing distinction.** v3 organizes
storage by plane. The measurements suggest a second axis that predicts
recoverable bytes better: whether an artifact is a record or a cache.
30.7 MB of this build is cache stored as if it were record.

One thing to retire: v1's "90% reduction" headline. Against the current
layout, 63.5% is what the measured, evaluation-safe program delivers.
Reaching 90% would require quantizing the durable record, which ADR 0003
forbids and v3 §17 explicitly rejects.

---

## 11. Reproducing These Numbers

```bash
python scripts/measure_storage.py                    # layered breakdown
python scripts/measure_storage.py --json             # machine-readable
python scripts/storage_simulate.py                   # measured candidate savings
python scripts/storage_simulate.py --keep            # keep transformed copies
```

Both accept `--index`, `--generation`, and `--repo-root`.
`storage_simulate.py` never modifies the published generation; it copies to a
temporary directory and removes it on exit.

Recommended follow-up before Phase D ships: extend the retrieval evaluation
harness to report recall against a pruned vector plane, so the one
behavior-changing recommendation in this report is gated on measurement
rather than on the favorable prior in §5.

---

## 12. Open Questions

1. **`entities.metadata_json` is 1.43 MB after removing the duplicated
   hash.** The `behavior` and `confidence` sub-objects are a per-entity
   analysis payload. Are they read at query time, or only written? If the
   former, they are a candidate for the codec plane; if the latter, for
   removal.
2. **5,452 Python chunks over 2.08 MB of Python** is roughly one chunk per
   380 bytes, against a configured `max_chunk_size` of 1000. Is the
   effective granularity intended, or are chunks being emitted per-symbol
   *and* per-window?
3. **Prose indexing is unbudgeted.** 577 markdown chunks cost 2.25 MB of
   vectors against 317 KB of text. Should documentation share the code
   corpus's embedding budget, or carry its own?
4. **Is the 100k-vector threshold for skipping the ANN index correct?** It
   should be measured against exhaustive-scan latency on real hardware
   rather than assumed.
