# Storage Footprint v4: Measured Audit and Execution Plan

**Status:** Approved for execution
**Date:** 2026-08-29
**Builds on:** `docs/archive/storage_optimization_2026.md` (v1),
`storage_optimization_2026_v2.md`, `storage_optimization_2026_v3.md`
**Closes:** v3 Phase 0 ("Measurement"), which was never executed against the
current SQLite/LanceDB artifact layout

---

## 0. How to Use This Document

This document is both the audit and the work order. It is written to be
executed without reference to the conversation that produced it.

- **§1–§2** state the result and the decisions that are already settled. Do
  not re-litigate §2; those questions were asked and answered.
- **§3–§10** are the evidence. Every number is measured unless marked
  **(modeled)**. Re-run §3's three commands before starting — if the numbers
  have moved, the plan still holds but the targets shift.
- **§11** is the execution plan: seven phases, each with the files to change,
  the change itself, how to verify it, and what "done" means. Phases are
  ordered by return per unit of risk and are designed to ship independently.
- **§12** is the measured end-state after each phase. Use it as the
  acceptance target.
- **§14–§16** are appendices: a code-anchor index, the schema deltas, and the
  questions this round resolved.

The repository is Python, `pytest`, `ruff`, `mypy`, `black`. Coverage floor is
80%. Every phase below lists its own tests; write them first.

---

## 1. Executive Summary

v1 through v3 proposed architectures. None of them measured the artifacts that
`knowcode build` actually writes, because the measurement tool still targeted
the retired JSON layout and failed on startup. Every storage number in v3 is a
planning range, not an observation.

This round measured a real first build. On this repository — 221 Python files,
2.08 MB of Python, 8.72 MB of all tracked files — one published generation is
**87.9 MB**: a **42x amplification over the Python source**, **10x over the
entire tracked tree**. Default retention is two generations, so a second build
takes the directory to ~176 MB.

The footprint is not dominated by irreducible index structure. Five specific,
fixable causes account for it:

| # | Cause | Nature | §|
|---|---|---|---|
| 1 | Every embedding stored twice at full fp32 width (52.5 MB) | one copy is a derived cache | §5 |
| 2 | The chunker emits a corpus that is both duplicated and incomplete | correctness defect | §6 |
| 3 | No embedding-selection policy — 1,046 chunks under 100 B cost 4.09 MB of vectors | unbudgeted policy | §7 |
| 4 | Absolute paths used as primary and foreign keys | encoding choice | §8 |
| 5 | Derived data persisted as if durable | `tokens_text`, `source_code`, duplicated hashes | §9 |

Cause 2 is the one this round newly surfaced, and it is not only a storage
problem. **39% of this repository's prose is not indexed at all**: markdown
documents are captured by a Python-oriented header extractor that stops at the
first line beginning with `import `, `from `, `class `, or `def ` — including
lines inside code fences. Meanwhile 502 markdown headings are stored as
standalone 33-byte chunks with no body, and 734.6 KB of method bodies are
stored twice because a class and each of its methods are chunked separately.
`ProseChunker` — which solves exactly this, and is fully tested — exists in the
tree and is not wired into the indexer.

**Measured outcome of the full program: 87.90 MB → 29.73 MB, a 66.2%
reduction**, with prose coverage rising from 65% to 96% of document bytes. The
first four phases reach **35.90 MB (59.2%)** with no change to what is
retrievable. Nothing in the program quantizes the durable record, which
ADR 0003 forbids.

---

## 2. Decisions of Record

Settled on 2026-08-29. Execute against these; do not reopen them.

### DR-1 — `entities.metadata_json` is retained

The `behavior` and `confidence` sub-objects stay persisted. They are read at
query time by three consumers (§16.1), and although they are deterministic
derived data, recomputing them on hydration would make every query depend on
the repository being present and readable at the same revision. That trades a
1.4 MB saving for a new runtime coupling in the retrieval path, and it would
degrade a generation used without its source tree — the CI-cached artifact case
that Phase C's portability fix is specifically meant to enable.

**Consequence:** the ~1.4 MB "recompute behavior on hydration" line is removed
from the plan. No end-state number changes, because it was never simulated and
was never included in a measured total.

**Still in scope:** stripping the *duplicated `content_hash`* from
`metadata_json`. That is a different thing — a key that repeats a first-class
column, not analysis payload. See §9 and C4/C5.

### DR-2 — One unified corpus, one embedding budget

Prose and code share a single chunk corpus, a single embedding budget, and a
single selection policy. Documentation does not get a separate quota.

**Rationale.** Retrieval is already unified — `search_engine.py` ranks all
chunks in one pass — so a split budget would create a ranking artifact with no
user-visible meaning: a prose chunk could be excluded while a strictly less
informative code chunk survives. A single threshold expressed in content bytes
is measurable, explainable, and applies the same rule to both. Where the
measurement later shows a type genuinely needs different treatment, tune the
per-type rule inside one planner rather than splitting the budget.

**Consequence:** `EmbeddingPlanner` (Phase E) takes the whole corpus, and
`ProseChunker` output lands in the same `chunks` table as code, distinguished
by `metadata_json.type`, not by a separate store.

### DR-3 — Fix the chunking

The chunker's granularity is not intended; it is a defect. Correct it before
tuning any policy on top of it. Specifically: class chunks stop carrying their
methods' bodies, prose is chunked by `ProseChunker` instead of by the Python
header extractor, and label-only chunks (`section`, `config_key`) are folded
into the unit that holds their content.

**Rationale.** Phase E's threshold is meaningless applied to a corpus whose
shape is wrong — it would prune heading-only chunks that should never have been
emitted, while leaving duplicated method bodies untouched. Fixing the corpus
first makes the policy a genuine quality/size trade rather than a workaround.

**Consequence:** Phase B is new, sits before the encoding work, and is the only
phase that fixes a retrieval correctness bug.

---

## 3. Method and Reproduction

Measurements come from the generation published in this repository,
`20260828T100515254661Z-fa1be334`, built with `voyage-code-3` at 1024
dimensions, `max_chunk_size=1000`, `overlap=100`.

```bash
python scripts/measure_storage.py        # where the bytes are
python scripts/storage_simulate.py       # what each encoding change recovers
python scripts/chunking_projection.py    # what the corrected corpus costs
```

All three accept `--index`, `--generation`, `--repo-root`, and `--json`. None
of them modify the published generation; the two simulators copy to a
temporary directory and remove it on exit unless `--keep` is passed.

What each measures:

- **`measure_storage.py`** — four layers: `stat()` bytes per artifact, page
  usage per table and index via `dbstat`, payload bytes per column via
  `LENGTH()`, and vector-plane geometry (count, width, bytes-per-text-byte).
- **`storage_simulate.py`** — applies each encoding and derived-data candidate
  to a throwaway copy, `VACUUM`s, and `stat()`s the result. Individual and
  composed.
- **`chunking_projection.py`** — reconstructs the corpus the corrected chunker
  would emit, writes it into a throwaway `chunks.db`, and measures it. Retained
  chunks carry their real `metadata_json`; `tokens_text` is recomputed with the
  real `tokenize_code`; embeddings are placeholder BLOBs of the true width
  (SQLite does not compress BLOBs, so file size is representative).

Numbers that could not be produced by transformation are marked **(modeled)**.

Note on units: this report uses file bytes. Disk usage is higher — the vector
index is spread across 273 fragments averaging 95 KB, each rounding up to a
filesystem block, so `du` reports 30.6 MB where `stat()` reports 28.4 MB.

---

## 4. Where the Bytes Are

### 4.1 Artifacts

| Artifact | Bytes | Share |
|---|---:|---:|
| `chunks.db` | 39.93 MB | 45.4% |
| `vectors.lancedb` | 28.43 MB | 32.4% |
| `knowledge.db` | 19.48 MB | 22.2% |
| manifests (`manifest.json`, `index_manifest.json`, `vectors.json`, `preflight_report.json`, `current.json`) | < 4 KB | ~0% |
| **Total** | **87.90 MB** | |

The control plane is already tiny — v3's "few MB for the manifest" target is
comfortably met. The entire problem is in the three data artifacts.

### 4.2 Column payload

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
| `entities.metadata_json` | 1.43 MB | duplicates `content_hash`; rest retained per DR-1 |
| `relationships.target_id` | 2.35 MB | absolute-path TEXT key |
| `relationships.source_id` | 2.02 MB | absolute-path TEXT key |
| `entities.entity_id` | 0.44 MB | absolute path prefix |
| `entities.content_hash` | 0.28 MB | 64-char hex |

### 4.3 Tables and indexes

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
storing those references as absolute-path TEXT in the table and then repeating
the same strings in both covering indexes.

The FTS index itself is well built: `content='chunks'` external-content mode
means `chunks_fts_data` is only 1.20 MB and stores no duplicate text. The waste
is one layer up, in `tokens_text`.

### 4.4 Corpus composition

Chunk counts and content bytes by `metadata_json.kind` / `.type`:

| Kind | Chunks | Content | Vectors | Amplification |
|---|---:|---:|---:|---:|
| `function` | 2,283 | 1,371.0 KB | 8.92 MB | 7x |
| `method` | 1,490 | 863.0 KB | 5.82 MB | 7x |
| `class` | 997 | 815.8 KB | 3.89 MB | 5x |
| `section` | 502 | 16.2 KB | 1.96 MB | **124x** |
| `variable` | 318 | 43.6 KB | 1.24 MB | 29x |
| `module_header` | 254 | 372.5 KB | 0.99 MB | 3x |
| `imports` | 215 | 56.9 KB | 0.84 MB | 15x |
| `config_key` | 68 | 0.6 KB | 0.27 MB | **463x** |
| `document` | 39 | 8.8 KB | 0.15 MB | 18x |

By language:

| Extension | Files | On disk | Chunk text | Ratio |
|---|---:|---:|---:|---:|
| `.py` | 220 | 2,028.5 KB | 3,217.4 KB | **1.59x** — text stored more than once |
| `.md` | 35 | 487.7 KB | 317.8 KB | **0.65x** — 35% of prose never indexed |

Those two ratios are §6.

---

## 5. Finding 1: Vectors Stored Twice (52.5 MB)

`chunks.db` holds 24.09 MB of fp32 embeddings. `vectors.lancedb` holds
25.30 MB of vector data plus 3.13 MB of bookkeeping. These are the same 6,166
vectors.

This is not accidental. ADR 0003 makes the SQLite BLOB the durable record, and
`indexer.py::_recover_vectors_from_chunks` (`indexer.py:381`) rebuilds the
vector store from committed chunk rows:

```python
chunk = self.chunk_repo.get(chunk_id)
if chunk is None or chunk.embedding is None:
    logger.error("Cannot rebuild vectors for %s: no durable embedding", chunk_id)
    return False
self.vector_store.upsert(chunk_id, chunk.embedding)
```

**The architectural fact this establishes: `vectors.lancedb` is a derived
cache, not a durable artifact.** 28.43 MB — 32% of the first build — is fully
reconstructible from bytes already on disk. Nothing requires it to be
published, retained per generation, or copied forward.

### 5.1 The bookkeeping is pure waste

`lancedb_vector_store.py` calls `self._table.add(rows)` per batch
(`lancedb_vector_store.py:291`) and never compacts or cleans versions. Neither
`compact_files` nor `cleanup_old_versions` appears anywhere in the module.
Result:

- 273 data fragments averaging 95 KB
- 274 superseded version manifests totaling 3.10 MB
- 273 transaction records totaling 0.03 MB

3.13 MB of that is recoverable by calling the compaction API that already
exists, with no design change at all. The fragmentation costs more than the
file bytes suggest: 546 of these files are small enough that block rounding
inflates them, which is why `du` reports 30.6 MB against 28.4 MB of content.

### 5.2 Width is a policy question, not a correctness one

v3 §8 is explicit that vectors are a ranking and navigation layer, never the
correctness layer, and equally explicit that quantization must not be called
lossless. Both hold. But that framing means the *derived ANN index* may be
narrower than the *durable record*.

Measured on this repository's own vectors (6,166 × 1024, L2-normalized, 300
real chunk vectors as queries, exhaustive fp32 top-10 as ground truth):

| ANN width | Size | recall@10 |
|---|---:|---:|
| fp32 | 24.09 MB | 1.0000 |
| fp16 | 12.04 MB | 0.9997 |
| int8 (per-vector scalar) | 6.02 MB | 0.9950 |
| binary (sign) | 0.75 MB | 0.8110 |
| binary + fp32 rescore of top-100 | 0.75 MB + durable | 0.9983 |

fp32 vectors are also nearly incompressible — zlib takes 24.09 MB to 22.31 MB,
a 7% gain — confirming v3 §5.3's "low benefit" classification. The lever on the
vector plane is count and width, never compression.

### 5.3 Direction

Keep the durable fp32 BLOB exactly as ADR 0003 specifies. Change the derived
plane. Below roughly 100k vectors, skip the ANN index entirely: an exhaustive
fp32 dot product over 6,166 × 1024 is a few million multiply-adds and completes
well under a millisecond. An ANN index at this scale buys nothing and costs
28 MB. Where an on-disk ANN index is warranted for large repositories, build it
int8 — 4x smaller at 0.995 recall@10, with the fp32 record still available for
exact rescoring.

Execution: A1 (compaction), D1 (index becomes a cache).

---

## 6. Finding 2: The Chunker Emits the Wrong Corpus

This is the finding that did not exist before this round, and the only one
that is a retrieval correctness defect rather than a storage inefficiency.

Three independent problems, all in `src/knowcode/indexing/chunker.py`.

### 6.1 Class bodies are stored twice

`process_parse_result` (`chunker.py:60`) calls `_chunk_entity` for every
non-module entity. A class and each of its methods are separate entities, and
`_chunk_entity` (`chunker.py:165`) appends `entity.source_code` for each. A
class's `source_code` contains its methods' source, so every method body is
stored once inside the class chunk and again as its own chunk.

Measured: **714.4 KB of method text is re-contained inside class chunks**, out
of 863.0 KB of method text in total — 83% duplication. Re-assembling the 997
class chunks into their 280 class entities and trimming each to its shell
(everything before the first member definition) reduces class content from
815.8 KB to **81.2 KB**.

The sliding window compounds it: 976 entities exceed `max_chunk_size` and
produce 2,907 chunks between them, with `overlap=100` duplicating a further
188.6 KB.

This is why `.py` chunk text is 1.59x the Python on disk.

### 6.2 Prose is chunked by a Python header extractor, and 39% is lost

`_emit_module_chunks` (`chunker.py:67`) runs for every file with source,
including markdown. `_extract_module_header` (`chunker.py:104`) walks lines and
**breaks at the first line starting with `import `, `from `, `class `, or
`def `** — a rule that is correct for Python and arbitrary for prose. In a
markdown document those prefixes occur inside code fences and in ordinary
sentences.

Everything after that line is never emitted as a chunk. Measured across the 35
markdown files in this repository:

| Document | On disk | Indexed | Coverage |
|---|---:|---:|---:|
| `knowcode-parser-concurrency-security-hardening.md` | 193,004 B | 22,648 B | 12% |
| `adr-0008-persistence-format-and-token-economics.md` | 22,795 B | 12,099 B | 53% |
| `telemetry.md` | 6,977 B | 2,014 B | 29% |
| `OPENAPI_FUNCTION_CALLING.md` | 6,282 B | 2,988 B | 48% |
| `ide-integration.md` | 9,226 B | 5,454 B | 59% |
| `mcp-contract.md` | 13,460 B | 10,354 B | 77% |

**191.8 KB — 39% of this repository's prose — is not reachable by retrieval at
all.** No error is raised; the content silently does not exist in the index.

Where the extractor does not trip, the opposite failure occurs: the *entire
document* becomes one chunk, averaging 8,559 bytes against a configured
`max_chunk_size` of 1,000, because `_emit_module_chunks` never applies the
window. `README.md` is a single 4,924-byte chunk covering 99% of the file.

### 6.3 Headings and config keys are stored as bodiless chunks

The markdown parser emits a `SECTION` entity per heading
(`markdown_parser.py:88`) and the YAML parser a `CONFIG_KEY` entity per key
(`yaml_parser.py:126`). `_chunk_entity` gives each its own chunk. With no
`source_code`, the content falls back to `entity.name` (`chunker.py:168`) — the
label alone.

- `section`: 502 chunks, 16.2 KB total, **33 bytes average**, 1.96 MB of
  vectors — 124x amplification.
- `config_key`: 68 chunks, 0.6 KB total, **9 bytes average**, 0.27 MB of
  vectors — 463x amplification.

Samples: `KnowCode` (8 B), `YAML (custom parser)` (20 B),
`4.4 Structural Fusion Retrieval` (31 B),
`Phase 3: Structural Graph Hardening` (35 B).

So prose retrieval currently offers a heading with no body, or a whole document
with a diluted embedding, and nothing in between — while the section bodies
that a reader actually wants are either absent or buried.

### 6.4 The fix already exists in the tree

`src/knowcode/indexing/prose_chunker.py` (845 lines) implements exactly the
right thing: heading-hierarchy splitting, atomic code fences and tables,
hard token bounds, parent-child links for small-to-big retrieval, sibling
merging so the corpus is not shredded into micro-chunks, faithful `line_range`
pointers, and a cheap no-LLM context header. It has two test modules covering
markdown and reStructuredText.

**It is referenced by nothing outside its own tests.** `indexer.py:91`
constructs `Chunker()` and nothing else.

Running it over the same 35 markdown files:

| | Chunks | Content | Coverage | Vectors | Amplification | Chunks < 100 B |
|---|---:|---:|---:|---:|---:|---:|
| current | 577 | 317.8 KB | 65% | 2.25 MB | 7.3x | 502 |
| `ProseChunker` | 524 | 468.9 KB | **96%** | 2.05 MB | 4.5x | 25 |

Fewer chunks, half again as much indexed prose, a smaller vector plane, and no
micro-chunks. This is the rare change that improves quality and size together.

### 6.5 Corrected corpus, measured

`chunking_projection.py` applies all three fixes and measures the resulting
`chunks.db`:

| | Chunks | Content | Vector plane | `chunks.db` |
|---|---:|---:|---:|---:|
| published | 6,166 | 3,548.3 KB | 24.09 MB | 39.93 MB |
| corrected | 5,328 | 2,964.3 KB | 20.81 MB | **35.45 MB** |

| Kind | Chunks | Content | Avg |
|---|---:|---:|---:|
| `function` | 2,283 | 1,371.0 KB | 614 B |
| `method` | 1,490 | 863.0 KB | 593 B |
| `prose` | 524 | 468.9 KB | 916 B |
| `variable` | 318 | 43.6 KB | 140 B |
| `class` | 280 | 81.2 KB | 296 B |
| `module_header` | 219 | 80.0 KB | 373 B |
| `imports` | 210 | 56.7 KB | 276 B |

4.48 MB recovered, prose coverage 65% → 96%, and the corpus a policy will
later be applied to is now a sane one.

Execution: Phase B.

---

## 7. Finding 3: No Embedding-Selection Policy

Every chunk gets a 4,096-byte vector regardless of how much text it holds.

| Content size | Chunks | Content | Vectors | Amplification |
|---|---:|---:|---:|---:|
| < 100 B | 1,046 | 38 KB | 4.09 MB | **110x** |
| 100–250 B | 821 | 144 KB | 3.21 MB | 23x |
| 250–500 B | 1,196 | 428 KB | 4.67 MB | 11x |
| 500–1000 B | 1,308 | 908 KB | 5.11 MB | 6x |
| >= 1000 B | 1,795 | 2,030 KB | 7.01 MB | 4x |

Phase B removes most of the <100 B bucket, because most of it is the
heading-only and key-only chunks of §6.3. What survives is a genuine policy
question: `imports` blocks (210 chunks, 56.7 KB) whose content is a list of
module paths that `relationships` already carries as import edges, and
`variable` chunks averaging 140 B.

Also measured: 118 chunks are byte-identical to another chunk, costing 0.46 MB
of duplicate vectors. Every chunk already carries a `content_hash`, so
deduplication needs no new machinery.

Applied to the **corrected** corpus, with one threshold for code and prose
alike (DR-2), skipping `imports`:

| Minimum content | Vectors | Vector plane |
|---|---:|---:|
| none | 5,118 | 19.99 MB |
| >= 100 B | 4,613 | 18.02 MB |
| >= 250 B | 3,796 | 14.83 MB |
| >= 400 B | 3,046 | 11.90 MB |

At 250 B the vector plane is 14.83 MB against today's 24.09 MB. This is the one
change in the program that alters what is retrievable, and the only one gated
on the evaluation harness.

Execution: Phase E.

---

## 8. Finding 4: Absolute Paths as Keys

Entity ids are absolute paths:

```
/Users/deepg/Desktop/KnowCode/.agent/rules/analysis-integrity.md::analysis-integrity
```

v1 flagged this in June 2026 — both the ~15% size cost and the portability
break. It is still present, and worse than v1 estimated, because the ids are
also foreign keys in `relationships` and are then repeated in two covering
indexes.

Two independent problems:

**The repo prefix.** 30 bytes repeated across roughly 27,620 stored path fields
plus every index that covers them. Measured saving from repo-relative paths
alone: **2.04 MB in `chunks.db`, 4.46 MB in `knowledge.db`**.

**TEXT keys in the edge table.** Measured saving from integer entity ids plus a
relationship-kind codebook: **6.80 MB, 34.9% of `knowledge.db`** — the single
largest lossless win available. This is v3 §5's codec plane applied where it
pays most.

Beyond bytes, both changes fix real defects. Absolute paths make a generation
non-portable across machines, checkouts, and containers — the artifact cannot
be cached in CI or shared between developers. Integer keys also make graph
traversal cheaper: comparing 4-byte integers instead of ~100-byte strings.

Execution: C1, C2.

---

## 9. Finding 5: Derived Data Persisted as Durable

**`chunks.tokens_text` — 2.97 MB.** Written as
`" ".join(tokenize_code(content))` and read back as `tokens_text.split()`.
Purely derived. It exists because FTS5 external-content mode needs a readable
column, and `tokenize_code` performs identifier splitting that `unicode61`
cannot reproduce from raw content. The fix is a contentless FTS5 table
(`content=''`): the term index survives, the text column does not. This forgoes
`snippet()` and `highlight()`, which the codebase does not use, and forgoes
rebuilding the FTS index without re-tokenizing — acceptable, since re-tokenizing
is deterministic and cheap. Measured: **5.64 MB (14.1%)**, more than the raw
column because dropping it also recovers page overhead.

**`entities.source_code` — 2.45 MB.** Read in exactly one place,
`service.py:1657`, and every field needed to resolve it from disk is already
stored: `file_path`, `line_start`, `line_end`, `content_hash`. Measured:
**4.77 MB (24.5%)**. This is v3's source-descriptor model, and v3 §11 argues it
is also *more correct* — resolving from disk with hash verification cannot
return stale source, whereas a persisted copy silently can.

**Duplicated `content_hash`.** Present as a first-class column and again inside
`metadata_json`. The two tables are not symmetric:

- `entities` **already has** a `content_hash TEXT` column. Stripping the
  duplicate from `entities.metadata_json` is a pure win: **2.09 MB**.
- `chunks` has **no** `content_hash` column. Stripping the key alone measures
  1.10 MB, but the lookup `get_chunk_id_by_hash`
  (`sqlite_chunk_repository.py:716`) depends on
  `json_extract(metadata_json, '$.content_hash')`, and `indexer.py:296` uses it
  to reuse durable embeddings across builds. Promoting the hash to a
  first-class `BLOB` column plus its index and then stripping the key measures
  **0.75 MB net** — the honest figure, and the one to plan against.

**`entities.content_hash` stored as 64-char hex — 1.76 MB.** `unhex()` yields
the identical digest in 32 bytes. No collision-resistance change, half the
bytes. (Chunk hashes are md5, so 16 bytes.)

**`chunks.content` — 3.47 MB.** A duplicate of the source tree, but unlike the
others it has live consumers: `reranker.py:154` and `:217`. Replacing it with
byte-range descriptors requires a source resolver, so it belongs to a later
phase. Measured if done: **6.39 MB (16.0%)**.

Execution: C3–C5, D2, D3, Phase F.

---

## 10. Finding 6: Retention Multiplies Everything

`DEFAULT_RETAINED_GENERATIONS = 2` (`generations.py:88`). Every byte above is
paid twice from the second build onward: 87.9 MB becomes ~176 MB, which is what
this repository currently holds.

Retention exists for a real reason — atomic generation publish with reader-safe
retirement (ADR 0004). But it currently retains *complete* generations
including the derived vector index, and copies forward data that has not
changed.

1. Once the vector index is derived rather than published (D1), retention costs
   32% less immediately, with no policy change.
2. Retention need not duplicate unchanged content. Content-addressed chunk and
   entity storage shared across generations, with generations holding only
   manifests and deltas, makes the marginal cost of a retained generation
   proportional to what actually changed.

Execution: D1 for (1), Phase G for (2).

---

## 11. Execution Plan

Seven phases. Each is independently shippable and independently revertible.
Phases A, C and D take nothing away from retrieval; B adds to it, by indexing
prose that is currently missing; only E narrows the semantic candidate set, and
only behind the evaluation harness.

**Global ordering constraint.** The generation manifest digests every artifact
with sha256 (`generations.py:484`, `digest_artifact`) and `publish_generation`
validates those digests before moving the pointer. Any byte-level change to an
artifact — `VACUUM`, LanceDB compaction — **must run before the manifest is
built**, inside the save path, not after. Getting this backwards produces a
digest mismatch at publish, not a silent corruption, so it fails loudly; but it
will fail every build until fixed.

**Schema versioning.** Three counters move independently:
`SqliteChunkRepository.SCHEMA_VERSION` (currently 2,
`sqlite_chunk_repository.py:75`), `SqliteKnowledgeStore.SCHEMA_VERSION`
(currently 1, `sqlite_knowledge_store.py:39`), and `Indexer.SCHEMA_VERSION`
(currently 2, `indexer.py:61`, mirrored into `index_manifest.json` at
`indexer.py:605`). Each phase below names which to bump. `MANIFEST_SCHEMA_VERSION`
(`generations.py:83`) only moves if the manifest's own shape changes; none of
these phases change it.

**Migration policy.** These artifacts are rebuildable from source. Do not write
in-place migrations for old generations — bump the schema version, let the
loader reject the mismatch with the existing legacy-schema error path
(`sqlite_chunk_repository.py:371`), and require a rebuild. Confirm this is
acceptable for any deployed index before shipping Phase C.

---

### Phase A — Free wins

No persisted contract changes, no schema bump. Both call APIs that already
exist.

| Item | Change | Saving |
|---|---|---:|
| **A1** | Call `compact_files()` and `cleanup_old_versions()` on the LanceDB table before the generation is digested | 3.13 MB |
| **A2** | `VACUUM` `chunks.db` and `knowledge.db` before the generation is digested | 2.03 MB |

**Files.** `src/knowcode/storage/lancedb_vector_store.py` (A1 — add a
`compact()` method beside `flush()` at `:215`, called from `save()` at `:383`);
`src/knowcode/storage/sqlite_chunk_repository.py` and
`sqlite_knowledge_store.py` (A2 — `VACUUM` in each `save()`/close path);
`src/knowcode/indexing/indexer.py:581` (`save()`) is the call site that must
run both before `atomic_write_json` of the manifest.

**Watch for.** `VACUUM` cannot run inside a transaction and requires no open
statements — ADR 0002 owns connection and transaction lifetime; follow it
rather than opening a side connection. LanceDB compaction rewrites fragments,
so it must complete before any digest is taken and before the store is closed.

**Verify.**
- Unit: `compact()` reduces fragment count on a table written in ≥ 2 batches.
- Unit: `VACUUM` runs on a repository with an open WAL and leaves row counts
  and a sample of `embedding` BLOBs byte-identical.
- Integration: build → publish succeeds → `manifest.json` digests match the
  on-disk artifacts (this is the ordering regression test).

**Acceptance.** 87.90 MB → **82.68 MB**. Fragment count < 10. Round-trip
retrieval results identical to the pre-change build.

---

### Phase B — Chunking correctness (DR-3, DR-2)

The only phase that fixes a retrieval bug. Bump
`SqliteChunkRepository.SCHEMA_VERSION` → 3 and `Indexer.SCHEMA_VERSION` → 3.

| Item | Change |
|---|---|
| **B1** | Class chunks carry the class shell only — signature, docstring, decorators, class-level attributes — not member bodies. Members are already chunked separately. |
| **B2** | Route `.md` and `.rst` through `ProseChunker`. `_emit_module_chunks` runs only for code files. |
| **B3** | Stop emitting label-only chunks. `SECTION` and `CONFIG_KEY` entities no longer produce their own chunk; their content is carried by the prose chunk or config chunk that contains them. |
| **B4** | Prose chunks land in the same `chunks` table as code, tagged `metadata_json.type = "prose"`, carrying `parent_id`, `context_header`, `line_range`, and `content_hash` from `ProseChunk`. |

**Files.**
- `src/knowcode/indexing/chunker.py` — `process_parse_result` (`:28`) gains a
  prose branch; `_chunk_entity` (`:148`) gains the class-shell rule and the
  label-only skip; `_emit_module_chunks` (`:67`) becomes code-only.
- `src/knowcode/indexing/indexer.py:91` — construct and hold a `ProseChunker`
  beside the `Chunker`; `:153` dispatches by file type.
- `src/knowcode/indexing/prose_chunker.py` — no change expected; it already
  produces everything B4 needs (`ProseChunk.id`, `.parent_id`,
  `.context_header`, `.content_hash`, `.start_line`, `.end_line`,
  `.embedding_text`).
- `src/knowcode/indexing/indexer.py:609` — `index_manifest.json` records
  `asdict(self.chunker.config)`; extend it to record the prose config too, so a
  loaded index can tell which chunking produced it.

**Design notes for the implementer.**
- *Class shell.* Do not regex the source at chunk time. The parser already
  knows an entity's `line_start`/`line_end` and its children; derive the shell
  from the entity model. `chunking_projection.py` uses a regex only because it
  works from stored text after the fact — that is a measurement shim, not the
  design.
- *Embedding text.* `ProseChunk.embedding_text` prepends the context header;
  `chunks.content` should store the body, and the embedding should be taken
  over `embedding_text`. `_embed_pending` (`indexer.py:309`) currently embeds
  `chunk.content` unconditionally — it needs a per-chunk embedding-text hook.
- *Unified corpus (DR-2).* One table, one budget. Do not add a prose-specific
  store or a prose-specific vector plane.
- *Windowing.* Code chunks keep the sliding window, but a window should not
  start mid-member when the entity model can give a better boundary. If that is
  more than a small change, leave it and note it — the measured target does not
  depend on it.

**Verify.**
- Unit: a class with three methods yields one class chunk whose content
  contains none of the three method bodies.
- Unit: a markdown document containing a fenced block whose first line is
  `from x import y` is fully chunked — assert total chunk content covers ≥ 95%
  of file bytes. *This is the §6.2 regression test; write it first and watch it
  fail.*
- Unit: no chunk is emitted for a `SECTION` or `CONFIG_KEY` entity.
- Unit: every prose chunk carries `type`, `parent_id`, `context_header`,
  `content_hash`, and a `line_range` that resolves to its own content in the
  source file.
- Integration: a build over `docs/` indexes ≥ 95% of document bytes.
- Regression: retrieval eval suite — code recall must not regress; prose recall
  is expected to improve.

**Acceptance.** `chunks.db` 39.44 MB → **35.45 MB**. Prose coverage 65% → 96%.
Chunk count 6,166 → ~5,328. Zero chunks under 20 bytes.
`python scripts/chunking_projection.py` after the rebuild should show the
published and corrected corpora converging.

---

### Phase C — Lossless encoding

Same information, fewer bytes, plus generation portability. Bump both store
schema versions.

| Item | Change | Measured in isolation |
|---|---|---:|
| **C1** | Repo-relative paths in `chunk_id`, `entity_id`, `file_path` (both artifacts) | 2.04 MB + 4.46 MB |
| **C2** | Integer-keyed `relationships` + relationship-kind codebook | 6.80 MB |
| **C3** | `entities.content_hash` as a 32-byte BLOB | 1.76 MB |
| **C4** | Drop duplicated `content_hash` from `entities.metadata_json` (column already exists) | 2.09 MB |
| **C5** | Promote `chunks.content_hash` to a first-class BLOB column + index, then drop the key from `metadata_json` | 0.75 MB net |

**Individual savings are not additive.** C2 also removes the absolute paths
inside the edge table, so C1 and C2 overlap heavily. Applied together, all of
C recovers **8.95 MB of `knowledge.db`** and **1.41 MB of `chunks.db`** on the
corrected corpus — not 19.9 MB. Plan against the composed figures in §12.

**Files.**
- `src/knowcode/storage/sqlite_knowledge_store.py` — schema, insert, hydrate.
  The `relationships` table becomes `(source_id INTEGER, target_id INTEGER,
  kind INTEGER, metadata_json TEXT)` with `eid(id, entity_id)` and
  `relkind(id, kind)` codebooks; both covering indexes are recreated on the
  integer columns. Exact DDL: §15.
- `src/knowcode/storage/sqlite_chunk_repository.py` — add `content_hash BLOB` +
  `idx_chunks_content_hash`; rewrite `get_chunk_id_by_hash` (`:716`) to
  `WHERE content_hash = ?` against `unhex(?)`.
- `src/knowcode/indexing/indexer.py:296` — `_reuse_durable_embeddings` calls
  `get_chunk_id_by_hash`; behavior is unchanged but it is the one consumer, so
  it anchors the contract test.
- Everywhere an id is constructed or consumed: `utils/entity_identity.py`,
  parsers (each builds `f"{file_path}::..."`), `graph_builder.py`,
  `retrieval/*`. Relative-path ids are a cross-cutting rename — grep for
  `"::"` and for `file_path` interpolation.

**Design notes.**
- Store paths relative to the *repository root*, resolved at load time against
  the root recorded in the manifest. Decide and document what happens when a
  generation is opened against a different root — reject on mismatch, or
  resolve lazily. ADR 0001 owns entity identity; extend it rather than
  inventing a parallel rule.
- The codebooks are per-generation, not global. They are rebuilt on every full
  build; incremental builds append.
- C2 changes graph traversal from string to integer comparison. Confirm the
  traversal API surfaces entity ids as strings to callers so nothing outside
  the store observes the integer keys.

**Verify.**
- Unit: an entity id round-trips through store and hydrate unchanged.
- Unit: relationship traversal returns identical edge sets before and after C2,
  compared as sorted `(source, kind, target)` string triples.
- Unit: `get_chunk_id_by_hash` returns the same chunk before and after C5.
- Contract: a generation built under `/tmp/a` and moved to `/tmp/b` loads and
  serves identical retrieval results. *This is the portability test C1 exists
  for — it currently cannot pass.*
- Property: for a corpus of random entities, integer-keyed and TEXT-keyed
  stores answer every traversal query identically.

**Acceptance.** `chunks.db` 35.45 → **34.04 MB**; `knowledge.db` 17.94 →
**10.53 MB**. Portability test green.

---

### Phase D — Stop persisting derived data

| Item | Change | Measured in isolation |
|---|---|---:|
| **D1** | Vector index becomes a rebuildable cache: not published, not retained, built on first semantic query or lazily at load | 28.43 MB |
| **D2** | `tokens_text` folded into a contentless FTS5 table (`content=''`) | 5.64 MB |
| **D3** | `entities.source_code` resolved from disk via `file_path` + `line_start`/`line_end`, verified against `content_hash` | 4.77 MB |

**`entities.metadata_json` is retained (DR-1). Do not strip `behavior` or
`confidence`.**

**Files.**
- D1: `src/knowcode/indexing/indexer.py` — `save()` (`:581`) stops writing
  `vectors/`; `load()` (`:617`) builds the store from durable BLOBs;
  `_recover_vectors_from_chunks` (`:381`) already contains the rebuild loop and
  becomes the primary path rather than the recovery path.
  `src/knowcode/indexing/generations.py` — `vectors.lancedb` leaves the digested
  artifact set; put the rebuilt cache outside `generations/` so retention never
  copies it. `src/knowcode/retrieval/search_engine.py` — for corpora under the
  exhaustive-scan threshold, skip the ANN index entirely (§5.3).
- D2: `src/knowcode/storage/sqlite_chunk_repository.py` — the three triggers
  (`chunks_ai`, `chunks_ad`, `chunks_au`) currently mirror `tokens_text` into
  the FTS table; with `content=''` they must insert the tokenized text directly
  and the column is dropped. DDL in §15.
- D3: `src/knowcode/storage/sqlite_knowledge_store.py` (drop the column),
  `src/knowcode/service.py:1657` (the one reader), and a new source resolver.
  `src/knowcode/analysis/live_source_loader.py` already exists — check whether
  it is the right home before adding another.

**Design notes.**
- D1 is the largest single win and the lowest-risk one, because the rebuild
  path is already implemented and already exercised. Sequence it first within
  the phase.
- Rebuild cost at this scale is a few seconds of pure CPU over 6,166 × 1024
  floats — no network, no embedding calls. Measure it and log it; if it is
  material for large repositories, cache the built index outside the generation
  keyed by the chunk-set digest (`generations.py:462`, `digest_ids`).
- D3 must fail closed: if the file is missing or the hash does not match,
  return no source rather than stale source. That is the v3 §11 correctness
  argument and the reason this is an improvement, not just a saving.

**Verify.**
- Unit: a generation with no `vectors.lancedb` serves semantic queries after
  rebuild, returning the same top-10 as one with the index present.
- Unit: FTS query results are identical before and after D2 across a corpus
  exercising identifier splitting (`snake_case`, `camelCase`, dotted paths).
- Unit: D3 returns identical source for an unmodified file; returns `None` and
  logs when the file is modified (hash mismatch) or absent.
- Integration: `knowcode build` → published generation contains no
  `vectors.lancedb` and no `source_code` column; retrieval eval suite
  unchanged.

**Acceptance.** `chunks.db` 34.04 → **28.51 MB**; `knowledge.db` 10.53 →
**7.39 MB**; `vectors.lancedb` **0 MB in the published generation**. Total
**35.90 MB — 59.2% below baseline**, with nothing changed about what is
retrievable.

---

### Phase E — Embedding-selection policy (gate on evals)

The only phase that changes retrieval behavior.

| Item | Change |
|---|---|
| **E1** | `EmbeddingPlanner`: a minimum-content threshold applied uniformly to code and prose (DR-2), default 250 B |
| **E2** | Skip `imports` chunks — `relationships` already carries import edges |
| **E3** | Deduplicate by `content_hash` — 118 chunks are byte-identical today |

**Files.** New `src/knowcode/indexing/embedding_planner.py`;
`src/knowcode/indexing/indexer.py:309` (`_embed_pending`) filters `pending`
through the planner before calling the provider; the threshold belongs in
`ChunkingConfig` or a sibling config in `data_models.py:137` so it lands in
`index_manifest.json` and a loaded index is self-describing.

**Design notes.**
- A chunk that is not embedded is still stored and still reachable through the
  exact symbol, path, and FTS planes. This narrows the *semantic candidate set*,
  not the index.
- Express the threshold in content bytes, not tokens — bytes are what the
  measurement is in, and the amplification argument is a byte argument.
- Make the planner's decision observable: record per-chunk why it was or was
  not embedded, so the eval harness can attribute a recall change.

**Verify.**
- Extend the retrieval evaluation harness to report recall@k against a pruned
  vector plane, sweeping the threshold across 0 / 100 / 250 / 400 B.
- Ship the threshold that holds recall within the harness's noise band; if 250 B
  costs measurable recall, fall back to 100 B and re-measure.
- Unit: an `imports` chunk is stored, is FTS-searchable, and has a NULL
  embedding.
- Unit: two byte-identical chunks share one embedding.

**Acceptance (at 250 B).** Vector plane 20.81 → **14.83 MB**; `chunks.db`
28.51 → **22.34 MB**; total **29.73 MB — 66.2% below baseline**. Recall within
the harness's noise band, *measured*, not assumed.

---

### Phase F — Source descriptors

Replace `chunks.content` with verified byte-range descriptors plus a source
resolver, extending D3's mechanism to the chunk plane. Consumers to migrate:
`reranker.py:154` and `:217`.

Worth **6.39 MB**, but the argument is correctness: v3 §2.3's verification rule
cannot be honored while snippets are served from a persisted copy that may be
stale.

Sequence after E, and only once D3's resolver has proven itself in production.

---

### Phase G — Cross-generation content addressing

Share unchanged chunk and entity storage across retained generations so the
marginal cost of retention is proportional to the delta (§10). Generations hold
manifests and deltas; content is addressed by hash in a shared store.

This is a larger change than everything above combined and should not start
until A–E have shipped and held.

---

## 12. Measured End-States

Running total after each phase. Every figure is `stat()` on a real file except
the two marked **(modeled)** — the vector index scaled by chunk count, which
Phase D drives to zero anyway.

| After phase | `chunks.db` | `knowledge.db` | `vectors.lancedb` | Total | vs baseline |
|---|---:|---:|---:|---:|---:|
| baseline | 39.93 | 19.48 | 28.43 | **87.90 MB** | — |
| A — free wins | 39.44 | 17.94 | 25.30 | **82.68 MB** | −5.9% |
| B — chunking correctness | 35.45 | 17.94 | 21.87 *(modeled)* | **75.26 MB** | −14.4% |
| C — lossless encoding | 34.04 | 10.53 | 21.87 *(modeled)* | **66.44 MB** | −24.4% |
| D — derived data | 28.51 | 7.39 | 0 | **35.90 MB** | **−59.2%** |
| E — embedding policy | 22.34 | 7.39 | 0 | **29.73 MB** | **−66.2%** |

Phases A–D change nothing about what is retrievable. Phase B *increases* what
is retrievable, by 191.8 KB of prose that is currently absent from the index.
Only Phase E narrows the semantic candidate set, and only behind the eval
harness.

At the end state, one generation is **14x the Python source** rather than 42x,
and **3.4x the entire tracked tree** rather than 10x. Two-generation retention
falls from ~176 MB to ~59 MB, and Phase G would make the second generation cost
only its delta.

If an on-disk ANN index is required rather than rebuilt on demand, an int8
plane adds 6.02 MB at a measured recall@10 of 0.995.

---

## 13. What This Changes in the v3 Doctrine

v3's architecture holds. Four corrections to its framing.

**The problem is not index structure; it is duplication and unfiltered policy.**
v3 planned exact postings, gram indexes, and codec tables that do not exist yet.
Meanwhile 60% of the current footprint is one artifact stored twice plus vectors
attached to text with no retrievable content. Phases A–E deliver a 66%
reduction without building any of v3's new index planes.

**Chunking is a first-class storage lever, and v3 did not treat it as one.**
v3 reasoned about what to store per chunk. It did not ask whether the chunk set
itself was correct. It was not: 83% of method text duplicated, 39% of prose
missing, 502 bodiless heading chunks. Corpus shape belongs upstream of every
per-chunk encoding decision.

**Vector count, not vector width, is the lever — and neither is compression.**
v3 §9's `EmbeddingPlanner` is the single highest-value unbuilt component, and
its cheapest form (a content threshold) captures most of the benefit. But it
must be applied to a corrected corpus, or it merely papers over §6.

**Derived-versus-durable is the organizing distinction.** v3 organizes storage
by plane. The measurements suggest a second axis that predicts recoverable bytes
better: whether an artifact is a record or a cache. 30.7 MB of this build is
cache stored as if it were record.

One thing to retire: v1's "90% reduction" headline. Against the current layout,
66% is what the measured, evaluation-safe program delivers. Reaching 90% would
require quantizing the durable record, which ADR 0003 forbids and v3 §17
explicitly rejects.

---

## 14. Appendix A: Code Anchor Index

Every file and line the plan touches, as of commit `054eb99`. Line numbers
drift; the symbol names are the durable reference.

**Chunking**
| Anchor | What |
|---|---|
| `indexing/chunker.py:28` | `Chunker.process_parse_result` — entry point, gains prose dispatch (B2) |
| `indexing/chunker.py:67` | `_emit_module_chunks` — becomes code-only (B2) |
| `indexing/chunker.py:104` | `_extract_module_header` — the `import`/`from`/`class`/`def` break that truncates prose (§6.2) |
| `indexing/chunker.py:148` | `_chunk_entity` — class-shell rule (B1), label-only skip (B3) |
| `indexing/chunker.py:168` | `content += entity.name` — the bodiless-chunk fallback (§6.3) |
| `indexing/chunker.py:194` | sliding window, `start += max_chunk_size - overlap` |
| `indexing/prose_chunker.py:110` | `ProseChunker` — built, tested, unwired |
| `parsers/markdown_parser.py:88` | `SECTION` entity per heading |
| `parsers/yaml_parser.py:126` | `CONFIG_KEY` entity per key |
| `data_models.py:137` | `ChunkingConfig` — `max_chunk_size=1000`, `overlap=100` |

**Indexing and publish**
| Anchor | What |
|---|---|
| `indexing/indexer.py:61` | `Indexer.SCHEMA_VERSION = 2` |
| `indexing/indexer.py:91` | `self.chunker = Chunker()` — the only construction site |
| `indexing/indexer.py:153` | `chunks = self.chunker.process_parse_result(...)` |
| `indexing/indexer.py:284` | `_reuse_durable_embeddings` |
| `indexing/indexer.py:296` | `get_chunk_id_by_hash` call site (C5) |
| `indexing/indexer.py:309` | `_embed_pending` — embeds `chunk.content`; planner hook (E1) |
| `indexing/indexer.py:381` | `_recover_vectors_from_chunks` — the rebuild path (D1) |
| `indexing/indexer.py:581` | `save()` — VACUUM/compaction must precede the manifest write (A1, A2) |
| `indexing/generations.py:83` | `MANIFEST_SCHEMA_VERSION = 3` |
| `indexing/generations.py:88` | `DEFAULT_RETAINED_GENERATIONS = 2` (§10) |
| `indexing/generations.py:462` | `digest_ids` — chunk-set digest, a cache key for D1 |
| `indexing/generations.py:484` | `digest_artifact` — why ordering matters |
| `indexing/generations.py:767` | `publish_generation` — validate then move the pointer |

**Storage**
| Anchor | What |
|---|---|
| `storage/sqlite_chunk_repository.py:75` | `SCHEMA_VERSION = 2` |
| `storage/sqlite_chunk_repository.py:371` | legacy-schema rejection path (migration policy) |
| `storage/sqlite_chunk_repository.py:716` | `get_chunk_id_by_hash` — the `json_extract` lookup (C5) |
| `storage/sqlite_knowledge_store.py:39` | `SCHEMA_VERSION = 1` |
| `storage/lancedb_vector_store.py:215` | `flush()` — where `compact()` belongs (A1) |
| `storage/lancedb_vector_store.py:291` | `self._table.add(rows)` — the un-compacted append |
| `storage/lancedb_vector_store.py:383` | `save()` — compaction call site (A1) |

**Retrieval and consumers**
| Anchor | What |
|---|---|
| `retrieval/reranker.py:154`, `:217` | the two `chunk.content` readers (Phase F) |
| `service.py:1657` | the one `entity.source_code` reader (D3) |
| `analysis/preflight.py:622` | reads `behavior["confidence"]` (retained, D1 of §2) |
| `analysis/context_synthesizer.py:318` | renders `behavior` (retained) |
| `analysis/documentation_synthesizer.py:272` | renders `behavior` (retained) |
| `analysis/live_source_loader.py` | existing source loader — check before writing a new resolver |

**Tools**
| Anchor | What |
|---|---|
| `scripts/measure_storage.py` | layered byte breakdown |
| `scripts/storage_simulate.py` | measured encoding and derived-data candidates |
| `scripts/chunking_projection.py` | measured corrected-corpus projection |

---

## 15. Appendix B: Schema Deltas

Current schemas, then the target. These are the exact transformations
`storage_simulate.py` measures, so a rebuilt artifact should land within a few
percent of the figures in §12.

### `chunks.db`

Current:

```sql
CREATE TABLE chunks (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id      TEXT NOT NULL UNIQUE,
    entity_id     TEXT NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    tokens_text   TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    file_path     TEXT NOT NULL DEFAULT '',
    embedding     BLOB,
    embedding_dim INTEGER
);
```

After C5 and D2:

```sql
-- C5: hash becomes a first-class column; the key leaves metadata_json.
ALTER TABLE chunks ADD COLUMN content_hash BLOB;
UPDATE chunks SET content_hash = unhex(json_extract(metadata_json, '$.content_hash'));
CREATE INDEX idx_chunks_content_hash ON chunks(content_hash);
-- then strip '$.content_hash' from every metadata_json payload

-- D2: contentless FTS5; tokens_text stops being stored.
DROP TABLE chunks_fts;
CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens_text, content='');
INSERT INTO chunks_fts(rowid, tokens_text) SELECT rowid, tokens_text FROM chunks;
ALTER TABLE chunks DROP COLUMN tokens_text;
```

The three triggers (`chunks_ai`, `chunks_ad`, `chunks_au`) currently read
`new.tokens_text` / `old.tokens_text` from the table. With the column gone they
must be rewritten to tokenize on the fly, or the FTS row must be written
explicitly by the repository alongside each chunk write. Prefer the latter —
explicit writes are easier to reason about than triggers that call into
application tokenization.

C1 rewrites `chunk_id`, `entity_id`, and `file_path` to repo-relative form. It
is a data change, not a DDL change.

### `knowledge.db`

Current:

```sql
CREATE TABLE entities (
    entity_id      TEXT UNIQUE NOT NULL,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    line_start     INT NOT NULL,
    line_end       INT NOT NULL,
    docstring      TEXT,
    signature      TEXT,
    source_code    TEXT,
    metadata_json  TEXT,
    content_hash   TEXT
);
CREATE TABLE relationships (
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX idx_relationships_source_kind ON relationships(source_id, kind);
CREATE INDEX idx_relationships_target_kind ON relationships(target_id, kind);
```

After C2 — integer edge keys plus codebooks:

```sql
CREATE TABLE eid(id INTEGER PRIMARY KEY, entity_id TEXT UNIQUE NOT NULL);
INSERT INTO eid(entity_id) SELECT DISTINCT source_id FROM relationships;
INSERT OR IGNORE INTO eid(entity_id) SELECT DISTINCT target_id FROM relationships;

CREATE TABLE relkind(id INTEGER PRIMARY KEY, kind TEXT UNIQUE NOT NULL);
INSERT INTO relkind(kind) SELECT DISTINCT kind FROM relationships;

CREATE TABLE rel_next(
    source_id     INTEGER NOT NULL,
    target_id     INTEGER NOT NULL,
    kind          INTEGER NOT NULL,
    metadata_json TEXT
);
INSERT INTO rel_next
SELECT s.id, t.id, k.id, NULLIF(r.metadata_json, '{}')
FROM relationships r
JOIN eid s     ON s.entity_id = r.source_id
JOIN eid t     ON t.entity_id = r.target_id
JOIN relkind k ON k.kind = r.kind;

DROP INDEX idx_relationships_source_kind;
DROP INDEX idx_relationships_target_kind;
DROP TABLE relationships;
ALTER TABLE rel_next RENAME TO relationships;
CREATE INDEX idx_relationships_source_kind ON relationships(source_id, kind);
CREATE INDEX idx_relationships_target_kind ON relationships(target_id, kind);
```

After C3 and D3:

```sql
-- C3: 64-char hex to 32-byte BLOB. Same digest, half the bytes.
UPDATE entities SET content_hash = unhex(content_hash)
WHERE content_hash IS NOT NULL AND LENGTH(content_hash) = 64;

-- D3: source resolved from disk, verified against content_hash.
ALTER TABLE entities DROP COLUMN source_code;
```

C4 strips `'$.content_hash'` from `entities.metadata_json`. **Everything else in
`entities.metadata_json` stays** — `behavior`, `confidence`, and the parser
annotations (`visibility`, `rust_type`, `is_test`, …). See §2 DR-1.

---

## 16. Appendix C: Questions This Round Resolved

### 16.1 Is `entities.metadata_json` read at query time?

**Yes — three consumers, all guard-checked projections.**

Traced through every module. `metadata_json` is written during indexing by
`annotate_entity_behavior()` (`analysis/behavior.py:180`, invoked from
`graph_builder.py:142`) and persisted verbatim as `json.dumps(entity.metadata)`.

It is read at query time by:

1. `analysis/preflight.py:622` (`_score_behavior_analyzability`) — reads
   `behavior["confidence"]`, thresholded at ≥ 0.65.
2. `analysis/context_synthesizer.py:318` (`_format_behavior`) — renders a
   markdown summary of the `BehaviorSummary` fields.
3. `analysis/documentation_synthesizer.py:272` (`_format_behavior`) — the same
   projection for generated docs.

It is **not** read by any retrieval or ranking code: `retrieval/search_engine.py`,
`retrieval/reranker.py`, `retrieval/exact_query_engine.py`, and
`retrieval/orchestrator.py` contain no reference to it. So retrieval *ordering*
does not depend on it; the *context assembled after retrieval* does.

**Decision: retain.** See §2 DR-1 for the reasoning. The alternative —
recomputing on hydration — was considered and rejected because it makes every
query depend on the source tree being present at the indexed revision, which
defeats the portable-artifact goal that Phase C's relative-path work exists to
enable.

### 16.2 Is the chunk granularity intended?

**No.** 5,452 Python chunks over 2.08 MB of Python is one chunk per 380 bytes
against a configured `max_chunk_size` of 1,000, and the cause is not windowing.
Classes and their methods are chunked independently, so 83% of method text is
stored twice; markdown headings and YAML keys become bodiless chunks; and prose
is chunked by a Python header extractor that silently drops 39% of it. See §6.

**Decision: fix it.** §2 DR-3, Phase B.

### 16.3 Should prose share the code corpus's embedding budget?

**Yes — one unified corpus, one budget.** See §2 DR-2 for the reasoning. In
practice this means `ProseChunker` output lands in the same `chunks` table,
distinguished by `metadata_json.type`, and `EmbeddingPlanner` applies one
content threshold to everything.

### 16.4 Is the 100k-vector threshold for skipping the ANN index correct?

**Still open.** It should be measured against exhaustive-scan latency on real
hardware rather than assumed. It does not block any phase: at this repository's
6,166 vectors the exhaustive scan is unambiguously correct, and the threshold
only matters once a repository approaches six figures. Measure it during D1 and
record the result here.

---

## 17. Execution Log

### Phase D1 — shipped 2026-08-29

**Taken first, out of the A-B-C-D order.** D1 is the largest single item in the
program and larger than Phases A, B and C combined. Those three recover
21.46 MB between them; D1 recovers the whole vector plane. Run first it
recovers the full plane rather than the 21.87 MB that would be left after A and
B had already shrunk it, and it retires A1, which is 3.13 MB of LanceDB
compaction applied to an artifact D1 removes from the published set entirely.
D1 is independent of B and C and forward-compatible with both.

**Correction to §3.** The generation these numbers came from,
`20260828T100515254661Z-fa1be334`, is no longer on disk, and `knowcode_index/`
in this tree is a stale pre-generation layout. The three reproduction commands
cannot run as written. Rebuild first. `create_embedding_provider` falls back to
`DummyEmbeddingProvider` when no API key is set, which produces 1024-dimension
L2-normalized vectors, the same geometry as `voyage-code-3`. That makes a
faithful storage baseline reproducible offline with no API calls:

```bash
git archive HEAD | tar -x -C /tmp/baseline && cd /tmp/baseline
knowcode build .
python scripts/measure_storage.py --index knowcode_index --repo-root .
```

**Corrections to §5 and §14.** `_recover_vectors_from_chunks` is scoped to the
ids one file transaction touched, not the whole corpus, so the full-corpus
rebuild D1 needs did not exist and had to be written. The code anchors in §5
and §14 are stale against the current tree. `_recover_vectors_from_chunks` is
at `indexer.py:457`, not `:381`, and `save()` is at `:836`, not `:581`.

**Baseline and result.** The tree grew from 221 to 239 Python files and 35 to
53 markdown files since the audit, so the absolute numbers are larger than §4's.

Both figures below are one build of the same 6,874-chunk corpus, the second
built by the new code in the same directory.

| Artifact | Before | After |
|---|---:|---:|
| `chunks.db` | 48.93 MB | 48.36 MB |
| `knowledge.db` | 31.93 MB | 30.51 MB |
| `vectors.lancedb` | 32.31 MB | **0 MB** |
| **Total** | **113.18 MB** | **78.87 MB** |

**32.31 MB of that is D1**, the whole vector plane, 28.5% of a generation from
one change. The remaining 2 MB is free-page variance between two SQLite builds,
not something this change caused; the baseline was measured with a hot
write-ahead log. Claim the 32.31 MB.

Two retained generations fall from about 226 MB to about 158 MB, because the
plane is no longer copied per generation. Nothing changed about what is
retrievable.

**What was built.**

- `SqliteChunkRepository.iter_embeddings` streams `(chunk_id, vector)` pairs,
  paginated by `rowid` under a fresh read lease per batch. One lease held
  across the whole stream would block `close()` for as long as a consumer kept
  the iterator alive.
- `Indexer.rebuild_vector_plane` clears the store and refills it from that
  stream, so it converges rather than accumulating.
- `Indexer.load` rebuilds when the generation carries no native vector
  artifact, and loads the persisted plane when it does. Old generations keep
  working.
- `Indexer.save` writes no vector artifact. `create_vector_store` no longer
  takes an `index_dir`, so no store can be pointed at a generation directory.
  For LanceDB that mattered more than dropping the save call, because
  `lancedb.connect` created the table inside the bundle as a side effect of
  construction.
- `SEMANTIC_ARTIFACTS` no longer carries the plane into incremental builds.
- The publication guard that required a native vector artifact is gone. The
  chunk/vector parity guard now counts non-null embeddings in `chunks.db`
  against the manifest, which is what ADR 0003 already required.

**Verification.** 1,677 unit and integration tests pass. On the 6,874-vector
corpus, with the plane as the only variable, a rebuilt plane and an
artifact-backed plane return identical results: 10 of 10 end-to-end searches
identical, top-25 raw vector ids identical, maximum score delta 0.000e+00.
Rebuild cost is 0.30 s, paid once per process open.

**Left undone.** An in-memory plane is the wrong default above roughly 100,000
vectors. §5.3's disk-backed int8 cache outside `generations/`, keyed by the
chunk-id digest, is not implemented, and neither is the exhaustive-scan path in
`search_engine.py`. Neither is needed for the saving above.

**Found, not fixed.** Both are in the
[engineering backlog](../engineering/backlog.md).

*Markdown documents are dropped from the index entirely* (BL-1). `MarkdownParser`
gives the document entity the id `<file>::<file-stem>` and each heading the id
`<file>::<heading-slug>`. When a document's H1 slugifies to its own filename,
the two collide, the chunker emits two chunks with the same id,
`validate_prepared_chunks` rejects the file, and `replace_file` keeps a previous
generation that does not exist on a first build. **17 of 53 tracked markdown
files, 259.9 KB, 32% of prose bytes**, including seven of eight ADRs and most of
the user guide, are absent from the index with only a `WARNING` line. Reproduce
with `knowcode build . 2>&1 | grep "duplicate chunk id"`.

This changes §6. Section 6.2 measured documents being *truncated* and derived
"39% of prose is unreachable" on the assumption that these files were indexed.
They are not indexed at all, so the real figure is worse, and Phase B must fix
the id collision as well as the chunking. B3 removes the `SECTION` chunk and so
incidentally removes the chunk collision, but the entity id collision in
`knowledge.db` survives it and needs its own item.

*A full rebuild can silently drop a watch commit* (BL-2).
`KnowCodeService.build_generation` publishes without `expect_current`, so a full
rebuild that began its scan before a file existed unconditionally overwrites a
watch generation that contained that file.
`tests/unit/service/test_watch_publication.py::test_concurrent_commits_and_publications_converge`
fails 13 runs in 40 on `main` because of it. D1 makes rebuilds faster and
widens the window to 22 in 40. The race is independent of this change.

**Revised order for the rest.** B moves to the front and grows: it now owns the
BL-1 id collision as well as the chunking, and it is the only remaining phase
that fixes a retrieval defect. A2 (`VACUUM`) still stands alone; its ~2 MB
estimate predates the observation that `knowledge.db` grew 64% for an 11%
entity increase, so size it against a measured `dbstat` free-page count
(BL-4) rather than the estimate. A1 is retired. C and E are unchanged, and D2
and D3 remain. The roadmap tracks this as
[P7](../roadmap.md).

| After phase | `chunks.db` | `knowledge.db` | vectors | Total |
|---|---:|---:|---:|---:|
| baseline (2026-08-29 tree) | 48.93 | 31.93 | 32.31 | **113.18 MB** |
| D1 — plane becomes derived | 48.36 | 30.51 | 0 | **78.87 MB** |
