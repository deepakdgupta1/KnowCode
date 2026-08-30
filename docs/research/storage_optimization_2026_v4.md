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

> **Start at §17's closing ledger, not at §11.** Most of this plan has shipped,
> two phases are rejected, and §11–§12's targets are sized against a corpus
> that Phase B replaced. The ledger says what is actually left, measured
> against the current artifact. Read §11 for how a phase was meant to work, not
> for whether it is still wanted.

- **§1–§2** state the result and the decisions that are already settled. Do
  not re-litigate §2; those questions were asked and answered. **§2's DR-4 is
  the newest and governs the rest:** no phase may cost retrieval quality,
  whatever the byte saving.
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
- **§17** is the execution log, one entry per phase as it shipped, and it ends
  with the ledger that closes the stream.

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

### DR-4 — Retrieval quality is a floor, not a variable to trade

Settled 2026-08-30, after three separate phases had proposed buying footprint
with recall. No phase in this plan may cost search or retrieval quality: not
recall, not precision, not the exactness of a mode that calls itself exact.
The size of the measured loss is not a mitigating factor. A phase that trades
quality is **rejected** — not deferred, not shipped behind a flag, not
re-sized until the loss looks affordable.

**Rationale.** Every number in this document is a byte count, and byte counts
argue better than recall numbers do: `18 MB` reads as a result and `0.9950`
reads as a rounding error. It is not one. Footprint is recoverable later — by
compression, by encoding, by stopping the same text being stored twice — and
this plan has already recovered 54 MB without touching a retrieval path.
Quality is not recoverable the same way. A caller handed the wrong ten rows
gets no signal that anything was lost, and no later phase gives those rows
back. Where the two compete, take the bytes that are free and leave the rest.

**Consequence, applied 2026-08-30.** Two items left the plan and the
[engineering backlog](../engineering/backlog.md) closed both as rejected
rather than fixed:

- **§5.2/§5.3's int8 ANN plane (BL-3).** `recall@10 0.9950` against exhaustive
  fp32's `1.0000` is half a percent of top-10 recall for 18 MB. If an on-disk
  plane is ever needed it is fp32; §5.2's binary-plus-rescore row is 0.9983 and
  is also not 1.0000.
- **§11's Phase F (BL-14).** It leaves `ExactQueryEngine` with no
  implementation, and the term index that would inherit the mode answers
  mid-identifier fragments at 14.6% recall at the limit the engine passes.

**Phase E survives, and is why this rule needed writing down.** E's premise is
that an unembedded chunk stays reachable through the exact, path, and FTS
planes. That premise is true only because F is rejected and the exact plane
stays — F would have removed one of the three legs E stands on. E ships on a
measured no-loss result or it does not ship, and "within the harness's noise
band" has to mean the harness could not detect a loss, never that a detected
loss was small enough to accept.

**Where the bytes come from instead.** Compressing `chunks.content` (`zlib`
2.38 MB, `zstandard` with a trained dictionary 3.05 MB) changes no plane, no
semantics, and no freshness contract. Not storing the chunk prefix its own body
already carries recovers a further 0.28 MB. Neither costs a row.

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

> **Overtaken by DR-4.** This section's heading is the claim DR-4 reverses.
> Width is not a free policy dial: every row below fp32 in the table above is a
> measured recall loss, and DR-4 rules those out whatever the byte saving. The
> measurements stand — they are what makes the rejection quantitative — but the
> only width this plan will ship is the one that reads `1.0000`.

### 5.3 Direction

Keep the durable fp32 BLOB exactly as ADR 0003 specifies. Change the derived
plane. Below roughly 100k vectors, skip the ANN index entirely: an exhaustive
fp32 dot product over 6,166 × 1024 is a few million multiply-adds and completes
well under a millisecond. An ANN index at this scale buys nothing and costs
28 MB.

~~Where an on-disk ANN index is warranted for large repositories, build it
int8 — 4x smaller at 0.995 recall@10, with the fp32 record still available for
exact rescoring.~~ **Rejected under DR-4, 2026-08-30.** 0.995 is not 1.000, and
no footprint number buys the difference. The exhaustive-scan half of this
direction is already what ships: `VectorStore` builds
`faiss.IndexIDMap2(faiss.IndexFlatIP(...))`, an exact fp32 scan. What is left
undone is a memory and open-time cost above roughly 100,000 vectors, which no
repository this project indexes has reached; re-file it as a footprint item
when one does, at a width that keeps recall@10 at 1.0000.

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

**`chunks.content` — 3.47 MB.** ~~A duplicate of the source tree, but unlike
the others it has live consumers: `reranker.py:154` and `:217`. Replacing it
with byte-range descriptors requires a source resolver, so it belongs to a
later phase. Measured if done: **6.39 MB (16.0%)**.~~

**Corrected 2026-08-30.** It is not a duplicate of the source tree: 55.9% of
chunks contain text that occurs nowhere in their file. It has nine consumers,
not two, and a sixth read path in `rebuild_fts()`. The reachable saving is
3.50 MB, not 6.39. And the column is the exact plane's only implementation, so
under DR-4 it stays. The bytes come from compression instead (§17, Phase F).

Execution: C3–C5, D2, D3. Phase F is rejected.

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
| **D2** | `tokens_text` folded into a contentless FTS5 table (`content=''`, `contentless_delete=1`; BL-13) | 4.64 MB re-measured |
| **D3** | `entities.source_code` resolved from disk via `file_path` + `line_start`/`line_end`, verified against `content_hash`, gated on the global `config.entity_source` setting (`disk`, the default, or `stored`) | 3.67 MB re-measured |

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
  (`chunks_ai`, `chunks_ad`, `chunks_au`) are removed; every mutating path
  writes or deletes its FTS row explicitly in the same transaction, a plain
  `DELETE` being exactly what `contentless_delete=1` buys (BL-13). The column
  is dropped, schema v4 fails closed on v3 databases, a runtime guard declares
  the SQLite 3.43 floor, and `rebuild_fts()` re-tokenizes from `content` as
  the replacement for the retired `'rebuild'` statement. DDL in §15.
- D3: `src/knowcode/config.py` — the global `entity_source` setting
  (`disk`/`stored`, default `disk`). `src/knowcode/storage/
  sqlite_knowledge_store.py` — one write seam nulls `source_code` when the
  mode is `disk`; the column stays in the schema so the setting flips per
  build without a migration. `src/knowcode/analysis/live_source_loader.py` —
  `load_verified_source` is the resolver (the right home; the read/slice half
  already lived there). `src/knowcode/service.py` — the one reader resolves
  when the stored copy is absent.

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
- Unit (the BL-13 pin): a deleted chunk's terms stop matching, and a replaced
  file's old terms stop matching while its new terms start. Asserting only
  that new chunks are findable passes on a broken build.
- Unit: D3 returns identical source for an unmodified file; returns `None`
  and warns when the file is modified (hash mismatch) or absent; returns
  `None` quietly for entities that never carried a snippet (their digest
  covers identity fields, and the stored copy was empty for them too); and
  `entity_source: stored` restores the persisted copy with its stale reads.
- Integration: `knowcode build` under the default `disk` mode publishes a
  generation whose `entities.source_code` is NULL throughout; retrieval eval
  suite unchanged.

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

> **Rejected 2026-08-30 under DR-4. This phase does not ship, in this form or
> a re-scoped one.** It cannot be built as written — 55.9% of chunks contain
> text that occurs nowhere in their file, and the reachable saving is 3.50 MB,
> not 6.39 — but that is not why it is rejected. It is rejected because it
> leaves `ExactQueryEngine` with no implementation: the term index that would
> inherit the mode answers mid-identifier fragments at 57.8% recall unbounded
> and 14.6% at the limit of 10 the engine passes, and a plane that answers them
> exactly costs 10.00 MB against the 4.32 MB column it replaces. Take the bytes
> from compression instead. See §17, Phase F, and backlog BL-14, closed as
> rejected. The correctness argument above — that a persisted snippet may be
> stale — is real and is answered by failing closed on a verified read where
> the text is addressable, which is D3 on the entity plane; it does not
> transfer to the chunk plane, where the text is not addressable.

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

~~If an on-disk ANN index is required rather than rebuilt on demand, an int8
plane adds 6.02 MB at a measured recall@10 of 0.995.~~ **Not an option under
DR-4** — 0.995 is a recall loss, and this plan does not trade recall for bytes.
An on-disk plane, if one is ever required, is fp32.

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

Every file the plan touches, by symbol. **This index carries no line numbers on
purpose (BL-5).** It used to, and they drifted by roughly 250 lines while the
prose beside them stayed true, so a reader who trusted a number landed in the
wrong function while the symbol name would have taken them to the right one.
A number that decays silently is worse than no number.

`tests/unit/docs/test_storage_plan_anchors.py` reads this table and fails when
a symbol here no longer exists. Every path is relative to `src/knowcode/`
unless it starts with `scripts/`. A `—` in the symbol column means the row is
about the file as a whole.

**Chunking**

| File | Symbol | What |
|---|---|---|
| `indexing/chunker.py` | `Chunker.process_parse_result` | entry point; dispatches prose (B2, shipped) |
| `indexing/chunker.py` | `Chunker._emit_module_chunks` | code-only since B2; skipped when no entity stands for the file (BL-6, BL-10) |
| `indexing/chunker.py` | `Chunker._extract_module_header` | the `import`/`from`/`class`/`def` break that truncated prose (§6.2) |
| `indexing/chunker.py` | `Chunker._chunk_entity` | class-shell rule (B1), label-only skip (B3) |
| `indexing/prose_chunker.py` | `ProseChunker` | wired by B2 |
| `parsers/markdown_parser.py` | `MarkdownParser.parse_file` | `SECTION` entity per heading, scoped by `HeadingScope` (BL-1) |
| `parsers/yaml_parser.py` | `YamlParser.parse_file` | `CONFIG_KEY` entity per key |
| `data_models.py` | `ChunkingConfig` | `max_chunk_size=1000`, `overlap=100` |

**Indexing and publish**

| File | Symbol | What |
|---|---|---|
| `indexing/indexer.py` | `Indexer.SCHEMA_VERSION` | the gate that forces a rebuild; **7**, moved by C5, BL-11 and BL-9 |
| `indexing/indexer.py` | `Indexer._reuse_durable_embeddings` | looks a chunk up by `content_hash` and attaches its vector without comparing content (BL-11) |
| `indexing/indexer.py` | `Indexer._embed_pending` | embeds `chunk.content`; the planner hook (E1) |
| `indexing/indexer.py` | `Indexer._recover_vectors_from_chunks` | the rebuild path (D1); scoped to one file transaction, not the corpus |
| `indexing/indexer.py` | `Indexer.save` | compaction must precede the manifest write (A2) |
| `indexing/generations.py` | `MANIFEST_SCHEMA_VERSION` | 3 |
| `indexing/generations.py` | `DEFAULT_RETAINED_GENERATIONS` | 2 (§10) |
| `indexing/generations.py` | `digest_ids` | id-set digest over a *set*; the manifest's parity key |
| `indexing/generations.py` | `digest_artifact` | why ordering matters |
| `indexing/generations.py` | `publish_generation` | validate, then move the pointer |

**Storage**

| File | Symbol | What |
|---|---|---|
| `storage/sqlite_chunk_repository.py` | `SqliteChunkRepository.SCHEMA_VERSION` | 4; gates nothing on its own, see §11's correction |
| `storage/sqlite_chunk_repository.py` | `SqliteChunkRepository.get_chunk_id_by_hash` | the reuse lookup; a column read since C5, not `json_extract` |
| `storage/sqlite_chunk_repository.py` | `SqliteChunkRepository.compact` | the staged rewrite, bracketed by its own losslessness witness (A2, BL-8) |
| `storage/sqlite_chunk_repository.py` | `SqliteChunkRepository._rewrite` | **where the deflate and the separator compaction go**, so they inherit that witness |
| `storage/sqlite_knowledge_store.py` | `SqliteKnowledgeStore.SCHEMA_VERSION` | 1 |
| `storage/sqlite_knowledge_store.py` | `SqliteKnowledgeStore.compact` | the `knowledge.db` half of the same pair |
| `storage/rewrite_witness.py` | `rows_preserved` | the bracket itself (BL-8) |
| `storage/lancedb_vector_store.py` | `LanceDBVectorStore.flush` | where `compact()` would have belonged (A1, retired by D1) |
| `storage/lancedb_vector_store.py` | `LanceDBVectorStore.save` | no longer a published artifact (D1) |

**Retrieval and consumers**

| File | Symbol | What |
|---|---|---|
| `retrieval/reranker.py` | `Reranker.rerank` | reads `chunk.content`; one of the readers Phase F would have broken (BL-14) |
| `retrieval/exact_query_engine.py` | `ExactQueryEngine.search_scored` | the whole exact plane; `LIKE` over `content`, literal since BL-15 |
| `service.py` | `KnowCodeService.get_entity_details` | the one `entity.source_code` reader (D3) |
| `analysis/preflight.py` | `_score_behavior_analyzability` | reads `behavior["confidence"]` (retained, §2 D1) |
| `analysis/context_synthesizer.py` | `ContextSynthesizer.synthesize` | renders `behavior` (retained) |
| `analysis/documentation_synthesizer.py` | `DocumentationSynthesizer._render_entity` | renders `behavior` (retained) |
| `analysis/live_source_loader.py` | `LiveSourceLoader` | the source resolver D3 reads through; bounded to the repository root (BL-25) |

**Tools**

| File | Symbol | What |
|---|---|---|
| `scripts/measure_storage.py` | — | layered byte breakdown |
| `scripts/storage_simulate.py` | — | measured encoding and derived-data candidates; schema-probing since BL-12 |
| `scripts/chunking_projection.py` | — | measured corrected-corpus projection |

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

-- D2: contentless FTS5; tokens_text stops being stored. contentless_delete=1
-- (SQLite 3.43+) is not optional: without it a contentless table refuses a
-- plain DELETE, and the 'delete' command needs the exact original tokens,
-- which this very migration removes (BL-13).
DROP TABLE chunks_fts;
CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens_text, content='', contentless_delete=1);
INSERT INTO chunks_fts(rowid, tokens_text) SELECT rowid, tokens_text FROM chunks;
ALTER TABLE chunks DROP COLUMN tokens_text;
```

The three triggers (`chunks_ai`, `chunks_ad`, `chunks_au`) are dropped rather
than rewritten. With the column gone they can neither read `old.tokens_text`
for a delete nor call into application tokenization for an insert, so the FTS
row is written explicitly by the repository alongside each chunk write and
removed with a plain `DELETE FROM chunks_fts WHERE rowid ...` — legal only
under `contentless_delete=1` — inside the same transaction as the chunk
delete. Every mutating repository path owns its FTS row.

`INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')` is not available
against a contentless table, so index repair becomes an explicit
re-tokenization pass over `chunks.content`, carried by the repository as
`rebuild_fts()`. Because `content` is stored — permanently, now that Phase F is
rejected under DR-4 — that pass reconstructs the whole term index without
touching the source tree. That property was one of the things F would have
taken away.

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

-- D3: source resolved from disk, verified against content_hash. As shipped,
-- the column is retained and written NULL under `entity_source: disk`
-- rather than dropped. The saving is the text, not the column shell, and a
-- retained column is what makes the setting a per-build choice: `stored`
-- writes it again on the next build with no migration either way.
UPDATE entities SET source_code = NULL;
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

**Still open, and now only a latency question.** It should be measured against
exhaustive-scan latency on real hardware rather than assumed. It does not block
any phase: at this repository's 6,166 vectors the exhaustive scan is
unambiguously correct, and the threshold only matters once a repository
approaches six figures.

DR-4 removed the half of this question that mattered. The threshold used to
decide *when to switch to an approximate plane*; with int8 rejected, crossing
it cannot change what is retrieved, only how long retrieval takes. So the
answer is a latency curve, and the plane above the threshold is still exact.

---

## 17. Execution Log

### Phase C1 — shipped 2026-08-29

Repo-relative id storage. Phase C is complete.

**The design departs from §11, and the departure is the point.** §11 calls C1 a
cross-cutting rename reaching every parser, `GraphBuilder`, and retrieval. ADR
0001 chose absolute ids precisely to avoid that, and a relative id inverts the
`Path(file_identity).is_absolute()` test `classify_endpoint_id` uses to
recognise an internal endpoint. C2 had already set a better precedent: integers
never leave the store. C1 follows it. Ids are absolute everywhere above the
storage layer, and the two stores encode them relative to a root they record.
The bytes are on disk, so the saving is the same. ADR 0010 has the decision.

**C1 is worth 3.15 MB at a real root, not the 3.86 MB recorded after C2.** The
`knowledge.db` half reproduced exactly, at 1.63 MB against the recorded 1.62.
The `chunks.db` half is 1.52 MB, not 2.24. The recorded figure was measured
where the root was long enough to make each stripped occurrence worth several
times what it is worth at a 29-character root.

**A simulated UPDATE fires the FTS triggers, and that hid two thirds of the
saving.** `chunks_fts` is an external-content FTS5 table over `chunks`, so
rewriting an id column makes the three sync triggers delete and reinsert every
row. `chunks_fts_data` grew 1,327,104 bytes and the measurement read 0.25 MB.
`tokens_text` holds no paths, so the real change never touches FTS. Suspending
the triggers for the rewrite moved the same measurement to 1.52 MB. A
simulation must not perturb a structure the real change leaves alone.

**`scripts/storage_simulate.py` is stale for post-C2 artifacts.** Its
`relative_paths` candidate for `knowledge.db` still strips
`relationships.source_id` and `relationships.target_id`, which C2 turned into
integer codebook keys, and it never touches `eid.entity_id`, where 17,024 of
the 22,374 rows holding root text now live. Anyone planning D or E from its
output is reading a number that no longer describes the artifact. Filed as
BL-12.

**Measured by paired build, same `git archive HEAD` extraction, two roots:**

| | 29-char root | 134-char root |
|---|---:|---:|
| `knowledge.db` before | 15,167,488 | 21,000,192 |
| `knowledge.db` after | 13,459,456 | 13,459,456 |
| `chunks.db` before | 44,494,848 | 50,053,120 |
| `chunks.db` after | 42,950,656 | 43,114,496 |
| generation before | 59,662,336 | 71,053,312 |
| generation after | **56,410,112** | **56,573,952** |

**Build location no longer sets the price.** The two roots differed by
11,390,976 bytes, 19.1% of a generation, on identical content. They now differ
by 163,840, or 0.29%. `knowledge.db` is byte-identical between them. The
residue is `metadata_json`, which carries a path in 1,017 chunk rows and is
data rather than identity, so the codec leaves it alone. This, not the 3.15 MB,
is what C1 was for.

**The manifest proves the encoding is lossless.** `read_entity_ids` and
`read_chunk_ids` hydrate through the recorded root, so a manifest digests ids
in the absolute form callers hold. Rebuilding the shallow extraction produced
the same `entity_ids` digest, the same `chunk_ids` digest, and the same counts
as the build before the change. A digest that survives a re-encoding is a
stronger witness than any row count, and unlike a count it cannot agree with
itself the way BL-8 describes.

**Two mutation probes, both verified live and verified removed.** The first
stopped relativizing the `eid` codebook only, which is the miss C2's shape
invites, and 11 of 12 pins went red. The second stopped relativizing
`chunks.file_path`, a third of that artifact's saving, and three pins went red
including the one that reads the stored column directly. Each probe was grepped
for in the file before the run and grepped for again after restoring from a
scratch copy, because a probe verified only by its own diff is not verified.

**Verified against the built artifact, not only the fixtures.** Every id column
in the published generation is relative, all 17,217 `eid` rows included, and
the graph reads back with 5,350 entities and 23,858 edges across seven kinds.
All three endpoint shapes round-trip, including the 14,480 `unresolved::` ids
whose percent-encoded path sits in the middle of the id rather than at its
front.

### Phase C2 — shipped 2026-08-29

Integer edge keys with two codebooks. C1 is the only Phase C item still open.

**Measured by paired build**, both artifacts rewritten to the real repository
root before sizing, because absolute-path ids make the build location worth 46%:

| | pre | C2+C3+C4+C5 |
|---|---:|---:|
| `knowledge.db` at the real root | 22,134,784 | 15,048,704 |
| entities | 5,304 | 5,304 |
| relationships | 23,683 | 23,683 |
| distinct (source, target, kind) | 20,322 | 20,322 |

7,086,080 bytes, 6.76 MB, of which C3 and C4 are 0.74. C2 itself is 6.0 MB,
which lands on the 6.04 MB the simulation predicted at a real root.

**The endpoint codebook is not `entities.rowid`, and that is the whole design.**
An import edge lands on an `external::` id and an unresolved call on an
`unresolved::` one. Neither has a row in `entities` to borrow a key from, so
`eid` is its own table. It holds 17,073 endpoints against 5,304 entities, so
more than two thirds of the endpoints in this graph are not entities at all.
Any scheme that keys edges off the entity table silently drops them.

**Integers never leave the store.** Every read still takes and returns entity id
strings. The codebooks are resolved inside the SQL, so the integer indexes do
the traversal rather than the caller paying a round trip. The queries moved out
to named module constants, because each carries two more joins than it did and
reading them inline stopped being possible.

**Build time did not move.** Interning both endpoints and the kind adds two
statements per edge, both `INSERT OR IGNORE` against a UNIQUE index. The full
corpus builds in the same six seconds it did before.

**Verified against the real artifact, not only the fixtures.** The unit pin
runs on four entities, which cannot show whether 23,683 edges survive
re-encoding. On the built graph, the caller set for the busiest call target is
identical to the set computed directly from the pre-change string-keyed table,
and the edge-kind histogram sums to the same 23,683.

**Two mutation probes, one of which was worthless and had to be redone.** The
first swapped source and target in the edge-to-text projection, and the pin
caught it. The second appended a SQL comment intending to stop interning the
target endpoint; the comment sat after the complete statement and changed
nothing, and the suite stayed green. It was replaced by collapsing every edge
to one codebook kind, confirmed by observing an edge read back under the wrong
kind, and the pin caught that. A probe verified only by its own diff is not
verified.

**What C1 is worth now.** Measured on top of everything above, at the real
root: 1.62 MB of `knowledge.db` and 2.24 MB of `chunks.db`, **3.86 MB** for the
generation. It is a cross-cutting change to every id in the index and it
carries two decisions the plan leaves open. What happens when a generation is
opened against a different root, reject or resolve lazily. And
`classify_endpoint_id` currently requires `Path(file_identity).is_absolute()`
for an endpoint to be INTERNAL, so relative ids invert an invariant that has
its own tests. BL-9 also waits on it, because the honest fix there is the scope
prefix ADR 1 describes, and both rewrite id encoding.

### Phase C3, C4, C5 — shipped 2026-08-29

The two content-hash items and the chunk hash column. C1 and C2 are still open.

**Measured by paired build.** One corpus extracted once, two source trees, the
only variable which `PYTHONPATH` the build ran under. Each arm asserted the
loaded module's `__file__` was its own tree before any number was taken.

| | pre | C3+C4+C5 |
|---|---:|---:|
| `knowledge.db` | 32,292,864 | 31,514,624 |
| `chunks.db` | 48,828,416 | 48,566,272 |
| entities | 5,304 | 5,304 |
| relationships | 23,683 | 23,683 |
| chunks | 6,562 | 6,562 |

1,040,384 bytes, 0.99 MB, with every row count unchanged.

**Measure `knowledge.db` at a realistic repository root or every number is
wrong.** The build above ran at a 116-character scratch path. The real root is
30 characters. Because entity ids and both edge endpoints are absolute paths,
that difference alone inflates the artifact from 21.11 MB to 30.80 MB, **46%**,
on identical content. Rewriting every id to the real root before measuring is
one `REPLACE`, and nothing downstream is trustworthy without it.

The same run measured at both roots, so the size of the artifact is the only
thing that changed:

| | at the 116-char scratch root | at the 30-char real root |
|---|---:|---:|
| baseline | 30.80 MB | 21.11 MB |
| C1 alone | 13.02 MB saved | 3.33 MB saved |
| C2 alone | 11.17 MB saved | 6.04 MB saved |
| C1 + C2 | 17.30 MB saved | 7.62 MB saved |

**At a real root §11's path and edge estimates hold, and the hash ones do
not.** C1's 4.46 MB against 3.33 measured, and C2's 6.80 against 6.04, are
close. C3's 1.76 MB against 0.23 and C4's 2.09 against 0.65 are not, and both
of those are per-entity constants: 5,304 rows times a few dozen bytes cannot
reach 2 MB on this corpus whatever the encoding. `VACUUM only` also now
measures 0.00 MB, so A2 has already taken the repacking that every pre-A2
per-item figure silently carried. Re-measure before planning against §11.

**C1 and C2 overlap, and C2 is the larger half.** Together they save 7.62 MB
where the parts sum to 9.37, so 1.75 MB is double-counted. C2's marginal value
after C1 is 4.29 MB; C1's marginal value after C2 is 1.58 MB. **C2 should run
first.** It is also the contained change, living entirely behind the store
boundary, where C1 rewrites every id in every parser.

**C1's case is not its bytes.** 3.33 MB standing alone, and 1.58 MB after C2,
is a weak size argument. The real case is the 46% above: an index built at a
deep path is far larger than the same index built at a shallow one, and today
the artifact's size depends on where it happened to be built. C1 makes that
independent, and it is what the portability test in §11 exists for.

**C4 is free because the column already won.** `_row_to_entity` overwrote
`metadata["content_hash"]` from the column after parsing `metadata_json`, so
the copy inside the JSON was never read. Dropping it changes no hydrated
entity. The chunk side is not like this: `_row_to_chunk` built metadata wholly
from `metadata_json`, so C5 had to re-inject the digest from the column or the
key would have gone missing rather than moved.

**A chunk digest is MD5, not SHA-256, and this decided C5.** §15's SQL assumes
`unhex()` of a 64-character digest into 32 bytes. `Chunker` has always emitted
`hashlib.md5(...).hexdigest()`, 32 characters wide. A packer that only handled
64-character digests left all 6,562 chunk rows stored as text, and C5 measured
a net **loss** of 36,864 bytes, because the new index cost 266,240 while the
table gave back only 229,376. Packing any even-length lowercase hex string, which
round-trips both widths exactly, turns that into a 262,144-byte saving: the
table gives up 425,984 and the index costs 163,840. The digest strength itself
is filed as BL-11.

**The index earns its 163,840 bytes.** Over 500 real digests from the built
generation, the reuse lookup runs in 62.8 ms against the indexed column, 1,803
ms against the pre-C5 `json_extract` over `metadata_json`, and 1,939 ms against
the same column with the index dropped. `_reuse_durable_embeddings` calls it
once per chunk, so this is the incremental build's inner loop.

**§11's instruction to bump both store schema versions is half wrong, again.**
Phase B established that `SqliteChunkRepository.SCHEMA_VERSION` is never
compared against a stored value. It moved to 3 for accuracy, and
`Indexer.SCHEMA_VERSION` moved to 4 because that is the one a generation
carries and the one that forces the rebuild C5 needs. Separately,
`_validate_existing_schema` now rejects a chunks table with no `content_hash`
column, so an old artifact fails at open with a rebuild message instead of
inside a query. That check is the structural form of the Phase B lesson.

**Verification.** 1,790 unit, integration and e2e tests pass. Every new
assertion was observed failing. The two encoding tests ran red on the
pre-change tree. The hydration re-injection and the new schema guard were each
probed by mutation, and in both cases the mutation was confirmed to have fired
by inspecting the loaded source before the test result was read. One test
initially failed for the wrong reason, passing an entity id where `replace_file`
takes a normalized file path, and was corrected rather than the code.

### Phase B — shipped 2026-08-29

Items B1 through B4, plus the two retrieval defects the backlog held against
this phase. This is the only phase that fixes a correctness bug rather than a
size one, and it is the last of those.

**Measured end to end.** One corpus, built twice in separate directories, the
chunking and prose identity the only variables. The pre-phase build is
`39e3668`, run with `PYTHONPATH` pointed at its own `src` and asserted to be
that tree before the numbers were taken.

| | pre-B | Phase B |
|---|---:|---:|
| chunks | 7,106 | 6,557 |
| chunk bytes | 4,190,466 | 3,770,537 |
| chunk bytes off the graph | 495,985 | **0** |
| markdown files with a chunk | 38 | **55** |
| prose bytes indexed | 357,245 | **822,542** |
| prose coverage | 41.6% | **95.7%** |
| entities | 5,215 | 5,303 |
| `chunks.db` | 51.53 MB | 48.35 MB |
| `knowledge.db` | 30.43 MB | 31.43 MB |
| **generation** | **81.96 MB** | **79.78 MB** |

2.18 MB net, and prose coverage from 41.6% to 95.7%. The size figure is small
because two of the four items add content rather than remove it. `chunks.db`
falls 3.18 MB against the plan's 3.99 MB estimate, on a tree that has grown
since §6.5 measured it. `knowledge.db` grows 1.00 MB, which is the honest cost
of the 17 documents that were not in the index at all.

**BL-1, the id collision, first.** `MarkdownParser` and `RstParser` built a
section id from the heading slug alone. A document whose H1 slugified to its
own filename collided with the document entity, and any two headings sharing a
title collided with each other. The chunker emitted duplicate chunk ids,
`validate_prepared_chunks` rejected the file, and on a first build the document
was absent with only a `WARNING`. 17 of 55 files. A section's qualified name
now carries its heading path, `<stem>.<h1>.<h2>`, which is what ADR 1 already
requires and what `module.Type.method` is for code, so a heading cannot collide
with its own document by construction. Sibling headings that genuinely share a
title take an ordinal.

**§6.2 is real and still fires, but not where a reading of it suggests.** No
markdown file in this tree is truncated by the import rule in the way §6.2
describes. The loss is concentrated in the large documents, and the aggregate
badly understates it:

| Document | pre-B | Phase B |
|---|---:|---:|
| `knowcode-parser-concurrency-security-hardening.md`, 193 KB | 13% | 99% |
| `hardening-contracts.md`, 87 KB | 31% | 96% |
| `adr-0007-protocol-and-artifact-evolution-inventory.md`, 79 KB | 23% | 96% |
| `storage_optimization_2026_v4.md`, 68 KB | 10% | 96% |

Several small documents measured *over* 100% before, because the whole-document
module chunk, the document chunk and the label-only section chunks all stored
the same bytes.

**B1 is the money.** 881,970 bytes, 92% of class chunk text, was member bodies
stored a second time. Trimming each class to the text before its first member
took `chunks.db` from 56.07 MB to 48.07 MB in isolation, 8.00 MB, the largest
single item in the phase. The cost is 72 source lines across 242 Python files,
4,115 bytes, that sit between members and are covered by no member chunk. 71
are comments, 8 of those divider rules, and exactly one is code. 214 to 1.

**BL-6 needed the prose route to have somewhere to land.** `ProseChunker` mints
section ids of its own shape, `<doc>::sec::<slug>::L<n>`, that no parser emits.
Storing those would have reproduced BL-6 in a new form. A prose chunk's stored
`entity_id` is the graph entity whose span its lines fall in, found by binary
search over section start lines, with the document anchoring any preamble.
Module chunks moved from the synthetic `<file>::module` to the file's own
MODULE or DOCUMENT entity.

**Correction to §11's schema versioning.** The plan says to bump
`SqliteChunkRepository.SCHEMA_VERSION` to 3 and let the loader reject the
mismatch through the legacy-schema error path. It does not reject anything.
`_ensure_schema_meta` writes the constant when no row exists and never compares
it against a stored one, and `_validate_existing_schema` checks columns rather
than the version. Probed directly: a database written at 2 reopens clean under
a constant of 3 and keeps recording 2. Only `Indexer.SCHEMA_VERSION`, mirrored
into `index_manifest.json`, actually gates, so that is the one that moved to 3.
Anything that wants the repository constant to mean something has to add the
comparison first.

**Structural guards, because these defects came back once already.** BL-6 was
written down in a research report in a previous round and lost. Two contracts
now hold the shape rather than the instance. Every parser must emit unique
entity ids within a file, asserted for all nine. No chunk may point at an
entity that does not exist, asserted for seven languages. The first of those
found BL-9 while it was being written.

**Found, not fixed.** Both in the
[engineering backlog](../engineering/backlog.md). BL-9, a top-level symbol
named after its own file collides with the file's MODULE entity, whose
qualified name is the stem; `src/knowcode/cli/cli.py` is affected and 48 of its
relationships land on the ambiguous id. Pinned as a strict `xfail` rather than
fixed, because the honest repair gives module-scoped symbols the scope prefix
ADR 1 already describes, which touches every parser and every id, and Phase C
rewrites id encoding anyway. BL-10, `VueParser` emits no entity for the file
itself, so 854 bytes across 5 test fixtures now go unindexed rather than being
stored against a synthetic id.

**Verification.** 1,780 unit, integration and e2e tests pass. `ruff check`,
`ruff format --check` and `mypy` are clean on every changed module. Each new
assertion was run against the pre-change tree and observed to fail; two that
passed under mutation were rewritten until they did not, one because it
asserted on chunk length where a short section body is legitimate, and one
because its fixture tripped the very extractor rule it meant to exercise.

**Revised order for the rest.** C leads now, and it inherits two things. BL-8
named it as the honest place to design a witness that survives every
publication path. §11's schema-versioning correction above means C cannot rely
on the repository constant to force a rebuild. D2 and D3 follow, then E behind
the evaluation harness. A1 is retired; A2, D1 and B are done.

| After phase | `chunks.db` | `knowledge.db` | vectors | Total |
|---|---:|---:|---:|---:|
| baseline (2026-08-29 tree) | 48.93 | 31.93 | 32.31 | **113.18 MB** |
| D1 — plane becomes derived | 48.36 | 30.51 | 0 | **78.87 MB** |
| B — chunking correctness | 48.35 | 31.43 | 0 | **79.78 MB** |

### Phase A2 — shipped 2026-08-29

Both SQLite artifacts are now vacuumed once, on the staged copy, after every
write and before the generation is digested. `chunks.db` and `knowledge.db`
each gained a `compact()` method, and `ChunkRepository` declares it with a
documented no-op default so the publication path can call it through the
abstraction. Three call sites use it. Two are in `KnowCodeService`, one for the
staged chunk store and one for the staged knowledge store; the third is in
`StagedGenerationWriter.publish`, which is the watch path.

**Measured, 3.79 MB.** The plan estimated 2.03 MB. BL-4 predicted the estimate
was low and it was, by 87%.

Both figures below are one corpus of 6,962 chunks and 5,129 entities, built
twice in the same directory by the same code. The only variable is whether
`compact()` does anything.

| Artifact | Uncompacted | Compacted | Saved |
|---|---:|---:|---:|
| `chunks.db` | 49.17 MB | 48.20 MB | 0.96 MB |
| `knowledge.db` | 31.50 MB | 28.68 MB | 2.82 MB |
| **Total** | **80.67 MB** | **76.88 MB** | **3.79 MB** |

**The bytes did not come from the freelist.** `chunks.db` held 16 free pages
and `knowledge.db` held none. Almost all of the saving is B-tree repacking,
which is why sizing this work from a free-page count alone was always going to
read low. `knowledge.db` gives up three times what `chunks.db` does because
bulk-inserting entities and relationships leaves its index pages loosely
packed, and that slack was then copied into every retained generation.

**Correction to §11.** The global ordering constraint is wrong about these two
artifacts. It says the manifest digests every artifact with sha256 and that
publication validates those digests, so a byte-level change must precede the
manifest. In fact `build_manifest` puts only `index_manifest.json` into
`artifact_names`. `chunks.db` and `knowledge.db` carry logical id digests and
counts instead, and every one of those is read out of the file *after* it is
compacted. Two consequences follow. A `VACUUM` running after the manifest is
built is not caught by anything. A compaction that lost rows would publish a
manifest that agrees with the damage, because `read_chunk_ids`,
`count_durable_embeddings`, and `_assert_chunk_vector_parity` all read the
compacted file. The rule stands for `index_manifest.json`, and it would have
stood for the LanceDB artifact A1 was going to compact. It never applied to the
databases.

Both claims were established by mutation probe rather than by reading. A
`VACUUM` moved to after the manifest was built changed nothing. A `compact()`
that deleted every seventh chunk row published and validated cleanly.

**What guards it instead.** At the store level, the compaction tests snapshot
every column of every row through a second connection and compare raw bytes,
so a re-encoded or dropped embedding fails. At the publication level,
`test_compaction_does_not_lose_rows_between_indexing_and_publication` compares
the published chunk count against the count the indexer reported before the
file was touched, which is the only witness in the build that does not come
from the compacted artifact. Both go red against the every-seventh-row
mutation. The first version of that test asserted the manifest digests matched
the artifacts on disk, exactly as §11 prescribed, and could not fail.

**No runtime guard was added.** `VACUUM` is one statement with SQLite's own
atomicity behind it, and a disk or I/O failure mid-rewrite rolls back and
raises. The realistic regression is somebody editing `compact()`, and the tests
cover that.

**Fixed in passing.** `tests/e2e/test_release_gate_limitations.py` still called
`create_vector_store(..., index_dir=...)`, an argument D1 removed. The e2e
suite was not part of D1's verification run, so the branch had been red since
then.

**Verification.** 1,734 unit, integration, and e2e tests pass. `ruff check`,
`ruff format --check`, and `mypy` are clean on every changed module.

| After phase | `chunks.db` | `knowledge.db` | vectors | Total |
|---|---:|---:|---:|---:|
| uncompacted (2026-08-29 tree) | 49.17 | 31.50 | 0 | **80.67 MB** |
| A2 — both databases vacuumed | 48.20 | 28.68 | 0 | **76.88 MB** |

**Revised order for the rest.** Unchanged by this phase. B still leads and
still owns the BL-1 id collision alongside the chunking, C follows, then D2 and
D3, then E behind the evaluation harness. A1 and A2 are both done with, A1
retired and A2 shipped.

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

**Resolved 2026-08-30, against the direction this entry assumed.** The int8
cache is rejected under DR-4 at `recall@10 0.9950`; backlog BL-3 is closed as
rejected, not deferred. The exhaustive-scan path turned out to be already
shipped — `VectorStore` builds `faiss.IndexIDMap2(faiss.IndexFlatIP(...))`,
which is an exact fp32 scan — so what this entry filed as two undone items was
one rejected item and one that was never missing.

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

---

### Phase D2 — shipped 2026-08-30

**Order taken.** BL-12's simulator was fixed first, because its own entry says
to fix it before sizing D, and because the defect BL-13 filed lived in the
simulator's shared `CONTENTLESS_FTS` DDL as much as in §15. Against the C1–C5
baseline the simulator re-measured the fold at **4.64 MB**, not §11's 5.64 MB,
which predates every C phase and a smaller tree. BL-13's fix then shipped as
part of D2 itself, in one changeset — there is no order in which D2 lands
first without it that does not silently corrupt search on the next re-index.

**What was built.**

- `chunks_fts` is contentless: `fts5(tokens_text, content='',
  contentless_delete=1, tokenize='unicode61')`. The option is the whole fix —
  a plain `DELETE` becomes legal, and the `'delete'` command that silently
  no-ops on wrong tokens is refused instead.
- The three sync triggers are gone. Every mutating path writes or deletes its
  FTS row explicitly inside the same transaction as its chunk row: `add` and
  `add_batch` purge the previous FTS rows for the ids they rewrite (INSERT OR
  REPLACE hands a rewrite a fresh rowid, and the stale row would keep matching
  the old terms forever), then insert the FTS row by joining the unique
  `chunk_id` back to whatever rowid survived; `remove_by_file` and
  `replace_file` delete the FTS rows by file before the chunk rows they
  resolve through.
- `tokens_text` is dropped from the table, the INSERT, and the hydration
  select. A hydrated chunk carries `tokens=[]`: tokens are an indexing-time
  derivation, and their durable form is now the FTS row. No reader under
  `src/` consumed them at query time.
- `rebuild_fts()` replaces the retired one-statement `'rebuild'`: it clears
  the index and re-tokenizes every row from `chunks.content` through
  `tokenize_code` — the same derivation the chunker used, so the rebuilt
  index equals the written one. Measured in isolation at 0.13 s over 3,022
  chunks and 1.8 MB of content, against 0.02 s for the old statement; the
  loss is sub-second repair, and repair now requires application code rather
  than any `sqlite3` shell.
- Schema v4. A database still storing `tokens_text` (v3) fails closed with a
  rebuild instruction, matching the v1 and v2 precedent.
- The SQLite floor is declared: `SqliteChunkRepository.MINIMUM_SQLITE_VERSION
  = (3, 43, 0)`, checked at open with a message naming the option and the
  linked runtime. The venv ships 3.50.4. The failure was already loud
  (`unrecognized option: "contentless_delete"`); now it is also actionable.

**Baseline and result.** Two builds of this tree, the second with D2 as the
only source change. The tree grew by 34 chunks between them (this change's own
tests and docs), which works slightly against the saving.

| Artifact | Before | After |
|---|---:|---:|
| `chunks.db` | 43.58 MB (6,712 chunks) | **38.90 MB** (6,746 chunks) |
| `knowledge.db` | 13.61 MB | 13.68 MB (unrelated entity growth) |

**4.46 MB off `chunks.db`** with 34 more chunks indexed; the simulator's
constant-corpus figure is 4.64 MB. `tokens_text` is gone from the schema, the
triggers are gone, and the FTS row count equals the chunk count.

**Verification.** 1,837 unit and integration tests pass (the one red herring,
`test_generation_hotswap` under load, is BL-7's documented flake and passes in
isolation). The BL-13 pin is `tests/unit/storage/test_contentless_fts.py`: a
removed file, a replaced file, and a re-added chunk id all stop matching their
old terms while a sibling chunk keeps matching — the assertion BL-13 says a
broken build cannot pass. The same module pins identifier splitting
(`snake_case`, `camelCase`, dotted paths, compound forms) as the before/after
gate, `rebuild_fts` reproducing the written index, `compact()` preserving the
contentless index through `VACUUM` (checked empirically first: a
`contentless_delete` table survives VACUUM with matches and bm25 intact), v3
failing closed, and the version floor refusing to open below it. On the
shipped artifact, FTS searches return the expected files, and the pre-D2
generation fails closed when opened by the new code.

**Left in Phase D.** D3 (`entities.source_code` resolved from disk, re-measured
at 3.70 MB by the fixed simulator) remains. Phase F stays gated on BL-14's
prerequisites; D2's landing makes none of them easier. *(Superseded
2026-08-30: F is rejected under DR-4, so the gate is moot.)*

---

### Phase D3 — shipped 2026-08-30, as a global configuration setting

**Deviation from §11/§15, on purpose.** D3 was specified as dropping
`entities.source_code`. It ships instead behind a global setting,
`config.entity_source: disk | stored` (default `disk`): the column stays in
the schema and is written NULL under `disk`, so the behavior is a per-build
choice with no migration in either direction. `stored` restores today's
persisted copy and its possibly-stale reads, one YAML line away. The saving
is the text, not the column shell, and the measured delta shows it.

**What was built.**

- `AppConfig.entity_source` (`disk`/`stored`, validated, default `disk`) —
  the setting rides the existing `config:` section beside `vector_backend`.
- One write seam in `SqliteKnowledgeStore._insert_entity_row` nulls
  `source_code` when the mode is `disk` — after the digest was computed
  upstream, so build, watch, and bulk paths are covered by construction. The
  service passes the mode at both store construction sites
  (`_open_store`, `_write_staged_knowledge_store`).
- `LiveSourceLoader.load_verified_source` is the resolver — the right home,
  as §11 suspected: the read/slice half already lived there. It re-reads the
  recorded span, canonicalizes, hashes, and serves the text only on an exact
  digest match. Everything else fails closed to `None`.
- `KnowCodeService.get_entity_details` resolves when the stored copy is
  absent; a row built under `stored` still serves its copy untouched.
- `compute_entity_fallback_hash` (extracted from
  `compute_entity_content_hash`) lets the resolver tell the two `None`s
  apart: MODULE/DOCUMENT-style entities hashed from identity fields never
  had a snippet, their stored copy was empty too, and their resolution is
  parity rather than drift — quiet, no warning.

**Baseline and result.** Two builds of this tree; the only change between
them is the mode.

| Artifact | Before (stored) | After (disk) |
|---|---:|---:|
| `knowledge.db` | 13.04 MB (4,032 rows with stored source) | **9.38 MB** (0 rows) |

**3.67 MB**, against the fixed simulator's 3.70 MB estimate. Served-source
coverage is unchanged: 4,045 of 5,471 entities (73.9%) resolve verified, and
the stored-copy build served 4,032 — the resolver serves, byte for byte, the
set of entities that ever had a snippet. The remaining 26% never carried one
(modules, documents, and BL-9-style merged ids), and served empty before D3
too. Of the sweep's warnings, the only genuine ones were the two source files
edited after the build — drift caught within minutes of indexing, which is
the failure mode working as designed.

**Verification.** 1,853 unit and integration tests pass. The loader matrix
(`tests/unit/analysis/test_live_hydration.py`) pins clean resolution, edited
body, shifted span, unrelated edit outside the span, deleted file, missing
digest, and the quiet snippet-less case. Service-level tests
(`tests/unit/service/test_entity_source_resolution.py`) pin the reader end to
end: `disk` resolves from the file and fails closed after drift; `stored`
serves the stale copy. The generation-publication tests, whose assertions are
about the persisted-copy semantics of preservation, now run under
`entity_source: stored` — the escape hatch doing exactly the job it exists
for.

**Correction, 2026-08-30: the reader coverage above is one consumer, not the
reader end.** `get_entity_details` is pinned in all three cases. `get_context`
was pinned in none, and it was broken: under the default `disk` mode on a
*fresh* index it served a context bundle with no source body at all, because
`KnowCodeService.get_context` built the `LiveSourceLoader` only when the index
was stale — a gate that was correct while a stored copy always existed and was
not correct after D3. That path carries the canonical MCP tool. Both *sides* of
the D3 seam were tested, the loader and the writer; the consumer between them
was not, which is the shape of gap the testing rules call a stubbed-boundary
miss.

Filed as BL-16 and fixed the same day. The synthesizer now picks the read by
why the text is absent — unverified slice for a stale index, `load_verified_source`
for a fresh one with no stored copy — and the service builds the loader
unconditionally. `tests/unit/service/test_entity_source_resolution.py` carries
the `get_context` half of the matrix, mutation-probed in both directions. D3's
reader coverage is now what this entry originally claimed.

**Mode transitions, pinned the same day.** The first cut read rows purely
adaptively — text served, NULL resolved — which made a `stored`-to-`disk`
flip quietly wrong: unchanged rows kept serving their unverified copies
indefinitely. The reader is mode-aware now: under `disk` the stored copy is
ignored and every read resolves verified, so a flip takes effect on the next
read, not the next rebuild; under `stored` a NULL row (from a `disk`-mode
build) still resolves, best effort.

Testing the transitions against incremental builds found a deeper fact those
tests pinned at the time: a watch commit copied `knowledge.db` unchanged, so
entity rows — text, NULL, and digest — advanced only on full builds. After a
watched edit, `disk` mode served `None` for that file's entities (the digest
predated the edit) where `stored` served the stale copy with no signal.

Filed as [BL-17](../engineering/backlog.md) (BL-16 when this entry was
written; renumbered for an id collision) and **fixed 2026-08-30** in the watch
pipeline rather than in D3, which is where it belonged: a file transaction now
rewrites the touched file's entity rows and the edges leaving them from the
same parse that produced its chunks. The transition tests are inverted
accordingly — a mode flip plus a watch commit now leaves a deliberately mixed
artifact, the touched file in the new mode and the rest in the old, which
reads correctly either way. D3's saving is unaffected. `tests/unit/service/test_entity_source_transitions.py`
holds the matrix: both flips over an incremental build, both flips over a
full rebuild, and the frozen-row assertion a watch commit must not violate.

**Phase D is complete.** D1, D2, and D3 have shipped; E follows behind its
eval gate, and F is rejected under DR-4 rather than deferred.

---

### Phase F — re-measured, then rejected 2026-08-30

Discharging BL-14's four prerequisites turned into a correction of the phase
itself, and then into DR-4. The full evidence and reproduction are in
`docs/engineering/backlog.md`, BL-14, closed as rejected; this records what the
plan has to stop saying. The decision is at the foot of this entry.

**§11's premise is false.** A chunk is not a slice of its file.
`ChunkBuilder._chunk_entity` prepends a reconstructed signature and re-quotes
the docstring around itself before appending the entity's source,
`_extract_imports` concatenates lines from across the file, and
`_extract_module_header` drops blank lines out of the span it copies. Over
generation `20260830T061050304877Z-8abc9f11`, resolved against a `git archive`
of the commit it indexed, only 2,991 of 6,782 chunks are a verbatim slice of
their file. **3,791 (55.9%) are not**, holding 54.7% of the stored chunk
bytes. Only prose is faithful, 4 of 1,042, because `ProseChunker` copies a real
line range. D3
worked because `entity.source_code` is a verbatim line span. Nothing carries
that property down to the chunk plane.

**§11's figure is stale by 2.89 MB.** Split every chunk into the longest suffix
that is on disk and the residue that is not, and 589,752 bytes have to stay
stored. Simulated: dropping the column outright saves 3.81 MB and is not
reachable, and the buildable form saves **3.50 MB**, not 6.39. Phase B took the
rest when a class chunk stopped carrying its members. §11's estimates have now
failed to reproduce in three phases; the standing instruction to re-measure
before planning against them holds here too.

**The plane F destroys costs three times what F saves.** An FTS5 `trigram`
index over the same text, contentless, answers a mid-identifier substring query
exactly as `LIKE` does and measures 10.00 MB against the 4.32 MB column it
replaces. The existing term index is not a substitute: measured against LIKE as
ground truth over three hundred mid-identifier fragments, it recovers 57.8%
unbounded and **14.6% at the limit of 10 the engine actually passes**, with
109 of 300 queries returning nothing. Whole-line literals survive at 90.8%.
`scripts/exact_plane_recall.py` is the measurement. So F plus an
honest replacement is a net increase of roughly 6 MB, and F without one drops
the mode.

**`rebuild_fts` is a read path §11 does not count.** It re-tokenizes every row
from `chunks.content`, which is what makes the term index reproducible from the
artifact alone with no tree access. F removes its input. Counting it, the
resolver threads through six repository methods and nine consumer call sites in
three modules, not the two the plan names, and the API's token-budget
arithmetic is among them.

**The resolver's cost is set by the reranker, not the result count.**
`orchestrator.py:188` requests `max(10, limit_entities * 5)` candidates and
`reranker.py:154` needs the text of all of them; 100 candidates touch 93
distinct files. Warm, that is 4.22 ms against 0.11 ms for the column. The cost
is linear in files opened rather than bytes read, so per-open latency is the
only variable a network filesystem changes: injecting 0.5 ms and 2 ms per open
gives 64.79 ms and 240.25 ms. The cold local cache is still unmeasured.
Darwin's `F_NOCACHE` is accepted and does nothing here, reading a 419 MB file
in 26.5 ms with the flag against 27.8 ms without, which is 16 GB/s and above
what the device can deliver. That prerequisite needs a host whose page cache
can be purged.

**The prefix is mostly a duplicate, which is what makes F unbuildable.** The
residue is not arbitrary. It is the reconstructed signature, which cannot match
the file because the parser drops the trailing colon, followed by the docstring
re-quoted around itself. Both then appear again in the entity source appended
straight after. Of 3,563 code chunks carrying a prefix, **2,616 (73.4%) have it
wholly present in the body it precedes** and 3,213 have it at least partly
present; 290,474 of the 536,057 prefix bytes are wholly redundant. This is B1's
defect in a second place, text stored twice inside one chunk, and it is the
reason 55.9% of chunks stopped being addressable. Not prepending what the body
already carries would recover about 0.28 MB and make most of the corpus
sliceable, so it is the prerequisite F actually has. It also changes what gets
embedded, so it belongs behind the same eval gate as E.

**Fail-closed is not a rare state.** A modified file makes every chunk in it
unresolvable while `chunks_fts` still matches its terms, so search reports a
hit that renders as nothing. The twelve paths edited during one working session
on this repository hold 518 chunks, 7.6% of the index.

**The cheaper way to the same bytes is compression.** It changes no plane, no
semantics and no freshness contract. `zlib` at level 6 saves 2.38 MB with no
new dependency, and the simulator carries it as a candidate; `zstandard` at level 10 with a 110 KB dictionary trained on the
corpus saves 3.05 MB, within 0.45 MB of everything F can reach. Chunks are
small and alike, which is why the shared dictionary is worth 0.68 MB over the
plain stream. The exact plane then scans decompressed text in Python at 18.0 ms
over all 6,782 chunks against 6.1 ms for `LIKE` today, still exact, with no
file opened.

**Recommendation, as written on the day of the measurement.** Stop prepending
text the body already carries, re-measure, and size what is left against
compression rather than against descriptors. If descriptors are still wanted
afterwards, ship them the way D3 shipped, as a global
`chunk_source: disk | stored` setting defaulting to `stored`, so the exact
plane, `rebuild_fts` and the API's budget arithmetic keep a text column until a
replacement plane exists and has been evaluated.

**The simulator was producing the stale number and now cannot.**
`scripts/storage_simulate.py` modelled this phase as
`ALTER TABLE chunks DROP COLUMN content`. It now resolves each chunk against
the indexed tree, keeps what a descriptor cannot address, and exits with an
error rather than reporting a number when the tree it is pointed at is not the
one that was indexed. Both candidates are listed, the unreachable ceiling
labelled as such. This is the second time this script has priced a change it
could not model (BL-12 was the first), and both times the number reached the
plan before anyone checked the model.

---

**Decision, 2026-08-30: rejected under DR-4, and the escape hatch withdrawn.**

The measurement above was written as a re-scoping. It is now a rejection, and
the difference is the reason DR-4 exists.

What changed is not a number. Every figure above stands. What changed is the
rule they are read against. Read as a sizing exercise, this entry says F is
worth 3.50 MB and needs prerequisites — which invites someone to discharge the
prerequisites and ship it. Read against DR-4, the sizing is beside the point:
F removes the exact plane's only implementation, the substitute answers
mid-identifier fragments at 14.6% recall at the limit the engine passes, and no
byte count buys that. The phase is closed.

**The `chunk_source: disk | stored` escape hatch is withdrawn with it.** A
default of `stored` looks safe, and that is what makes it the wrong shape here.
It ships the descriptor path, its resolver, and its fail-closed rendering into
nine consumers and six repository methods, then leaves them switched off — so
the plane's cost is paid in code that nothing exercises, and the day someone
flips the setting for footprint they take the recall loss with it. D3 could
carry a setting because both of its modes serve correct text: `disk` fails
closed on a verified read, `stored` serves the copy. F has no such pair. Its
second mode is *worse retrieval*, and DR-4 does not make room for a mode like
that, defaulted off or not.

**What survives this entry, and is worth doing.** The prefix duplication is a
real defect on its own terms — 2,616 of 3,563 code chunks prepend text their
own body already carries, 290,474 redundant bytes, B1's defect in a second
place. It is worth about 0.28 MB and it is why 55.9% of chunks stopped being
sliceable. It is not filed in the backlog, because removing it changes what
gets embedded and therefore what the semantic plane retrieves; under DR-4 that
puts it behind the same eval gate as E, and E is roadmap work with a gate
already attached. Size it there, not here.

**What replaces F.** Compression of the same column: `zlib` at level 6 for
2.38 MB with no new dependency, `zstandard` at level 10 with a trained
dictionary for 3.05 MB — within 0.45 MB of everything F could ever have
reached, changing no plane, no semantics, and no freshness contract. Neither is
scheduled yet. Whoever schedules one starts from
`python scripts/storage_simulate.py --index knowcode_index --repo-root .`,
which carries both as candidates.

---

### Stream ledger — 2026-08-30, what is left and what closes it

Written to answer one question: what remains in this plan that DR-4 permits.
Every figure is `stat()` on generation `20260830T061050304877Z-8abc9f11` or a
simulator run against it, not an estimate carried forward from §11.

**Where the artifact stands.** 46.70 MB — `chunks.db` 37.32 MB, `knowledge.db`
9.38 MB, no vector artifact. §12 projected 35.90 MB after Phase D. The 10.80 MB
gap is not a phase that under-delivered: every phase was measured on landing
and hit its number. It is that §12's projections run against the pre-B corpus,
and B indexed 2.3x the prose. Whoever re-opens §12 should re-baseline it rather
than reconcile it; the table describes a corpus that no longer exists.

**Phase ledger.**

| Phase | State |
|---|---|
| A1 | Retired. D1 stopped publishing the LanceDB artifact A1 was going to compact. |
| A2, B, C1–C5, D1, D2, D3 | Shipped. |
| E | Open, gated on P1's evaluation harness. Not lossless by construction. |
| F | **Rejected** under DR-4, with its `chunk_source` escape hatch. |
| G | Open. The only large lossless item left, and not inside a generation. |

**Remaining and permitted — 2.53 MB, 5.4% of the generation.**

| Item | Saving | Shape |
|---|---:|---|
| Deflate `chunks.content` | **2.38 MB** | The DR-4 replacement for F. `zstandard` level 10 with a trained dictionary reaches 3.05 MB and adds a dependency; `zlib` level 6 adds none. Costs a decompress per chunk read and moves the exact scan into Python — 18.0 ms against 6.1 ms over 6,782 chunks, still exact, no file opened. |
| Compact `metadata_json` separators | **0.15 MB** | 144,439 raw bytes over 11,284 rows. Three sites dump with Python's default `", "` / `": "`: `sqlite_knowledge_store.py:389`, `:442`, `sqlite_chunk_repository.py:595`. No schema change, no version bump, no reader affected. |

That is the whole of the in-generation headroom. It is not a shortlist.

**What DR-4 puts out of reach, and why it is not an oversight.**

*Durable fp32 embeddings — 26.72 MB, 57% of the generation.* This is the
largest number the simulator prints and it is not available. The row that
prints it is `ALTER TABLE chunks DROP COLUMN embedding`: a deletion, not a
relocation. There is nowhere to relocate to. The raw payload is 26.49 MB across
6,782 vectors, so SQLite's overhead on those BLOBs is 0.23 MB, and §5.2
measures fp32 as 7% compressible. The bytes *are* the vectors. ADR 0003 keeps
them and DR-4 agrees. (The simulator's label used to read "vectors moved out of
SQLite", which invited exactly the wrong reading; it now says what the code
does.)

*Phase E — 7.59 MB.* Narrows the semantic candidate set. Permitted only on a
measured no-loss result, which is a P1 dependency rather than a storage one.

*Phase F — 3.50 MB, BL-3 — 18 MB.* Rejected. See DR-4.

**The lever that is left is retention, not the generation.** Two retained
generations hold **97 MB** for a 46.70 MB index — three hours apart, and nearly
all of it identical bytes. Phase G is lossless by construction: content
addressed by hash, stored once, generations holding manifests and deltas. It is
worth more than every remaining in-generation item combined, and it is the only
thing between here and closing this plan. §11 says not to start it until A–E
have shipped and held. A–D have; E is gated on P1; F is gone. So G is either
adopted as its own workstream or the plan retires without it.

**Three items in this stream that save nothing and still block a clean close.**
[BL-11](../engineering/backlog.md) — chunks are content-addressed by MD5, and a
collision hands one chunk another chunk's vector through
`_reuse_durable_embeddings`. [BL-8](../engineering/backlog.md) — a manifest
cannot witness row loss in either database. [BL-5](../engineering/backlog.md) —
this document's own code anchors are stale. The first is a correctness defect
in the storage layer; the plan is not honestly closed while it is open.

**Exit condition.** Ship the deflate and the separators, fix BL-11, then decide
G. At that point one generation is roughly 44 MB, every remaining lever costs
retrieval quality, and this document has nothing left to say.

### BL-8 and BL-11 — closed 2026-08-31, and what that costs the budget

The stream ledger above names three items that save nothing and still block a
clean close. Two of them are now shut. BL-5, this document's stale code
anchors, is the only one left.

**BL-11 shipped. A chunk is content-addressed by SHA-256.** All four
`hashlib.md5` sites in `Chunker` are gone, and the prose route stops
re-hashing bytes `ProseChunker` has already digested. `Indexer.SCHEMA_VERSION`
moves 5 to 6, because the digest is the embedding-reuse key that
`_reuse_durable_embeddings` looks a chunk up by, so every generation rebuilds
once rather than mixing widths.

**It costs 0.24 MB, and that is the first phase in this plan to spend rather
than save.** Measured by rewriting every `content_hash` in generation
`20260830T061050304877Z-8abc9f11` to the real SHA-256 of the row's own
`content` and vacuuming both copies: 39,129,088 bytes to 39,366,656, **+237,568
bytes, 0.61%**. That is 108,512 bytes of column across 6,782 rows and the rest
in the index over it. Spend it. DR-4 rejects a saving that costs retrieval
quality, and the symmetric rule holds here: a collision in this column hands one
chunk another chunk's vector and the chunk still retrieves, so the bytes buy the
plane's correctness.

**§6 and the C5 record still say chunk hashes are MD5.** Both statements were
true when written and neither is edited. Read them as history: C5's packer
decision turned on the two widths differing, and `pack_content_hash` stays
width-agnostic even now that nothing emits 32 characters, because narrowing it
to one width is exactly what made C5 a net loss of 36,864 bytes the first time.

**BL-8 shipped, and its premise was half wrong.** `compact()` now brackets
itself: it reads one digest per row set from its own writer connection before
it does anything and compares after, so no number it checks is derived from the
damaged artifact. Nothing is threaded in from the caller, which is why it holds
on all three publication paths where §11's suggested chunk-count fix could not.
The probe that proved the hole also corrected the item. On the full-build path
`knowledge.db` was never blind, because `entity_ids` comes from the in-memory
`GraphBuilder` at `service.py:957` and an entity-dropping probe was already
refused as `entity id digest mismatch`. Only `StagedGenerationWriter.publish`
reads entity ids back out of the artifact. `chunks.db` was blind everywhere.

**The bracket costs 118 ms per publication**, measured with paired interleaved
runs on the same generation: 29 ms on a 39.1 MB `chunks.db` and 89 ms on a
9.8 MB `knowledge.db`. The `knowledge.db` half dominates because it scans
24,417 edges twice.

**Consequence for the remaining work.** The deflate of `chunks.content` and the
`metadata_json` separator compaction both rewrite a staged artifact. Put them
inside `_rewrite()` on the store that owns the file and they inherit the
losslessness witness. Put them beside it and they do not.
