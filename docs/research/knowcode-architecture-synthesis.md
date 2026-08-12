# KnowCode — Re-Architecture Roadmap

*Consolidated from design discussion. Scope: scaling code-intelligence retrieval to very large (50M+ LOC) repositories under a tight memory budget while preserving strict correctness guarantees.*

---

## 0. Operating Constraints & Core Principle

**Target & budget**

- **Scale:** 50M+ LOC (~3–10M indexed chunks at symbol granularity).
- **Memory:** ≤ 16 GB RAM for the query-serving process — *the binding constraint*.
- **Latency:** p99 < 10 s; no per-query disk-I/O limit.
- **Storage:** unconstrained.
- **Freshness:** daily overnight incremental indexer; ≤ 24 h staleness acceptable.
- **Authority:** the source tree is authoritative; correctness verified via robust invalidation, *not* per-query whole-tree comparison.

**The organizing principle everything hangs on: two contracts, one resolver.**

- **Exact layer (lossless, mechanically verifiable):** exact text + location come from reading the *live source tree* at query time. You cannot return stale text if you read it fresh.
- **Empirical layer (best-effort discovery):** semantic/vector + lexical retrieval produce *candidate IDs only*. Every candidate is materialized through the exact layer before it is returned.

**Consequence:** the discovery layer can be arbitrarily lossy (quantization, stale embeddings) and still never compromise text correctness — it can only affect *which* chunks surface, never *what* a surfaced chunk says.

**Hard boundary established in discussion:** *"semantic search finds all and only relevant chunks 100% of the time" is not achievable.* Relevance is not mechanically defined, embeddings are lossy by construction, and turning a ranked list into a set requires an imperfect threshold. 100% precision+recall is reachable only when "relevant" becomes a **decidable predicate** (exact symbol / structural / regex queries) — which belongs in the *exact* layer, not the semantic one.

---

## 1. Current Solution (As-Built)

### 1.1 Embedding & Retrieval

- **Embedding model:** `voyage-code-3` (1024-dim, code-specialized), with correct asymmetric `input_type="document"` vs `"query"`. Provider cascade: explicit config → app config w/ API key → `DummyEmbeddingProvider` fallback.
- **Dense arm:** FAISS `IndexFlatIP` — flat inner-product, i.e. brute-force, exact, **all vectors resident in RAM**.
- **Sparse arm:** `InMemoryChunkRepository.search_by_tokens` — raw set-intersection overlap (`len(query_set & set(chunk.tokens))`). Labelled BM25 in docstrings; it is not.
- **Fusion:** `HybridIndex.search` — Reciprocal Rank Fusion (RRF, K=60) with an `alpha=0.2` dense-weight blend by default (sparse-heavy; configurable via `config.hybrid_alpha`).
- **Tokenizer:** code-aware — splits camelCase/snake_case, strips punctuation, lowercases, filters single-char tokens. Each chunk stores pre-computed `tokens: list[str]`.

### 1.2 Storage & Persistence

Entirely in-memory (plain Python dicts), serialized to flat files. **No database anywhere** in `src/`.

| File | Format | Contents |
|---|---|---|
| `chunks.json` | JSON | Chunk metadata: id, entity_id, content, tokens, metadata (embeddings excluded) |
| `vectors.index` | FAISS binary | Dense flat-IP index |
| `vectors.json` | JSON | id_map (int → chunk_id) + dimension |
| `index_manifest.json` | JSON | Schema version, embedding & chunking config |
| `knowcode_knowledge.json` | JSON | Entities + relationships graph |

On load, the **entire** state is deserialized and hydrated into memory before the first query.

### 1.3 What's Already Right (preserve)

- Code-specialized embedder with correct query/document asymmetry — getting this wrong silently craters recall.
- Dense / sparse / RRF hybrid **architecture** already wired — the expensive structural work is done.
- Metadata and vectors already split across separate files — **the migration seams are pre-drawn**.
- Code-aware tokenization foundation.

---

## 2. Gaps Identified

| # | Gap | Category | Consequence |
|---|---|---|---|
| **G1** | FAISS flat = all vectors resident, brute-force | Memory/Scale | ~20 GB at 5M × 1024-dim float32 — blows the 16 GB budget |
| **G2** | Full-JSON deserialization at startup | Startup | O(n) cold-start; everything hydrated before first query |
| **G3** | No indexed token search at rest | Retrieval/Perf | Sparse arm linearly scans all chunks on every query |
| **G4** | Sparse arm is set-intersection, not BM25 | Retrieval/Quality | No IDF / TF-saturation / length-norm; common tokens (`self`, `return`) drown rare identifiers (`reconcile_ledger`) — **destroys the exact-identifier signal that is the entire reason to run hybrid on code** |
| **G5** | No lazy/partial loading | Scale | Cannot load a file/subtree without hydrating the universe |
| **G6** | No concurrency/atomicity | Reliability | `json.dump` has no locking, journaling, or atomic-write semantics |
| **G7** | Redundant source storage | Storage | Source text held in source tree *and* `chunks.json` (historically also knowledge.json) |
| **G8** | Verbose JSON + absolute string IDs | Storage/Perf | Bloated artifacts, slow parse, no O(1) integer handles |
| **G9** | Blend weight tuned against a broken sparse arm | Retrieval/Quality | `alpha=0.5` reflects compensating for noise, not real signal |
| **G10** | No reranking stage | Retrieval/Quality | Fused top-k never refined by a deeper relevance signal |
| **G11** | Location/freshness robustness not formalized | Correctness | Line-drift can mis-locate snippets between indexer runs if byte/line offsets are trusted blindly |

*G9–G11 are latent — they bite after the bigger items are fixed, or at scale.*

---

## 3. Candidate Solution Approaches

### 3.1 Metadata & Sparse Search → SQLite + FTS5

Replace the flat-file + in-memory-dict design for metadata with a single SQLite DB.

- **Real BM25** via FTS5's built-in `bm25()` (fixes G3, G4). IDF and length-normalization are present at FTS5 defaults — this alone kills all three of G4's sub-problems.
  - *Caveat:* FTS5 hardcodes k1=1.2, b=0.75 (not runtime-tunable). Fine for the bulk win; revisit only if measurement demands — then Tantivy (tunable BM25, separate index) or custom scoring over the `fts5vocab` table.
- **Lazy row-level access** eliminates O(n) hydration (fixes G2, G5).
- **WAL mode** + transaction-per-batch → atomic, crash-safe writes with concurrent reads (fixes G6) — exactly the "one overnight writer, many daytime readers" shape.
- **Dense integer IDs** + JSON metadata column replace verbose absolute-ID JSON (fixes G8); the FAISS id_map folds into a `faiss_idx` column (one fewer file).
- **Keep the code-aware tokenizer in Python:** pre-tokenize (camelCase/snake split, **plus index both subtokens *and* the original compound** — `getUserById` → `get user by id getuserbyid`), space-join, feed to an **external-content** FTS5 table (`tokenize='unicode61'`). Your tokenization drives the BM25 stats; FTS5 does the inverted index + math; external-content = zero text duplication.
- **Do NOT** store the FAISS index in SQLite as a BLOB — that reintroduces full-load. Vectors stay a separate mmap-friendly file.

### 3.2 Dense Vector Index → DiskANN/Vamana + RaBitQ

Replace FAISS flat (fixes G1).

- **Graph-based disk-resident index (Vamana / DiskANN family)** — current SOTA for large-scale, memory-frugal ANN: compressed vectors + graph in RAM, full-precision vectors on SSD; ~5–10× more points/node than HNSW at high recall. Matches the ≤16 GB-RAM / disk-unconstrained shape exactly.
- **RaBitQ quantization** for the in-RAM tier — supersedes PQ: no codebook training, unbiased distance estimator with a rigorous error bound (PQ has none and fails on some datasets). At ~1 bit/dim, 1024-dim vectors → **~128 B each → ~0.64 GB resident at 5M chunks** (vs ~20 GB raw float32). Multi-bit variant trades memory for recall.
- **Dynamic variants** (FreshDiskANN, LSM-VEC) matter: static graph indexes lose recall under repeated insert/delete — relevant to daily incremental updates. (OOD-DiskANN is notable — NL-query-vs-code-corpus is an out-of-distribution scenario.)
- **Engines:** LanceDB (columnar + on-disk vectors, ships RaBitQ; aligns with the SQLite-style metadata design) or Qdrant (turnkey hybrid + rerank).

### 3.3 Retrieval Paradigm → Hybrid + Reranking

- **Hybrid (dense + real-BM25 sparse) via RRF** is SOTA consensus — beats single-method by ~5–15% generally, far more on recall in some settings. For code the win is outsized because BM25 nails exact identifiers that dense embeddings miss.
  - *Why nothing is currently on fire:* RRF is rank-only, so a bad arm's contribution is **bounded** to `1/(K+rank)` — which is why the broken sparse arm degrades gracefully and appears "carried by dense" rather than tanking the system.
- **Re-tune the blend weight after fixing BM25** (fixes G9) — the optimum shifts toward sparse once it carries real signal. Same applies to k1/b if you move off FTS5: tune on code, not prose defaults.
- **Reranking stage** on the fused top-k (fixes G10): cross-encoder (e.g. Voyage rerank-2.5) or late-interaction/ColBERT. The p99 < 10 s budget makes "retrieve top-1000 → rerank top-100/200" essentially free. *(Caveat: rerankers don't always help — measure.)*

### 3.4 Correctness → Source-Tree-Authoritative + Invalidation

- **Read exact text/location live from the source tree** at query time (fixes G7; this is the exact layer).
- **Tiered location resolution** (addresses G11):
  1. *Fast path* — file content-hash matches indexed hash → trust stored byte offsets.
  2. *Slow path* — file changed → re-resolve symbol by qualified name.
  3. *Fallback* — content-fingerprint search; if the chunk is gone, report "changed/not found" rather than returning unverified text.
  - Use byte offsets over line numbers. **Invariant: never return text you can't mechanically verify is current.**
- The token stream and pointers stored in SQLite are *derived index data*, not authoritative source — consistent with eventually dropping redundant `content`.

### 3.5 Exact / Decidable Query Engine (for queries that need 100% guarantees)

For the class of queries where "all and only" is required *and* decidable — exact symbol lookup, all-callers/callees, implementors of X, type-based queries, regex/literal search:

- **Structured indexes** (symbol table, call edges, type signatures, references) answer these with provable completeness — *conditional on the soundness of the per-language fact extractor*. Graph completeness is not assumed unless proven for the language/feature.
- **Trigram index (Zoekt / Google Code Search pattern)** for literal/regex: trigrams yield a candidate superset with provably zero false negatives, then the regex runs against live source to remove false positives → **mechanically-provable 100% precision *and* recall**. Same philosophy as source-tree-authoritative: index narrows, source verifies. The trigram/lexical index also doubles as the sparse arm of hybrid.

### 3.6 Incremental Indexing

- **git diff** for changed-file detection (free, exact). **Content-hash** per file/chunk serves as *both* the invalidation key *and* the embedding-cache key — only genuinely changed chunks are re-embedded.
- **Base + delta** index pattern: large base rebuilt periodically, small delta updated nightly, queries merge both, periodic compaction — bounds nightly cost.

---

## 4. Recommendations & Prioritization

Prioritized by **leverage ÷ effort**, sequenced so each ships independently (no big-bang rewrite). The existing three-file split makes these genuinely decoupled migrations.

| Priority | Action | Impact | Effort | Closes |
|---|---|---|---|---|
| **P0** | `SqliteChunkRepository` (same protocol) + FTS5 BM25 + WAL + lazy access | ★★★ | Low | G2, G3, G4, G5, G6, G8 |
| **P1** | Re-tune RRF blend + add reranker (Voyage rerank-2.5) on fused top-k | ★★★ | Low | G9, G10 |
| **P2** | `KnowledgeStore` → SQLite (nodes + edges, recursive CTEs) | ★★ | Med | graph load + subtree (rest of G2/G5) |
| **P3** | FAISS flat → DiskANN/Vamana + RaBitQ (dynamic variant) | ★★★ | High | G1 |
| **P4** | Source-tree-authoritative reads + tiered location resolution | ★★ | Med | G7, G11 |
| **P5** | Decidable structured-query + trigram index (exact layer) | ★★ | High | enables 100%-guarantee queries |
| **X-cut** | Incremental indexer: git-diff + content-hash embedding cache + base/delta | ★★ | Med | freshness / re-embed cost |
| **X-cut** | Measurement: ablate dense-only vs hybrid vs BM25-hybrid on held-out real queries (nDCG@10, recall@50) | — | Low | validates every change above |

**Why P0 is first.** Single highest-leverage, lowest-effort move: converts the placeholder sparse arm into real BM25 (your fastest quality jump) *and* retires the startup, lazy-load, and concurrency gaps in one migration. It also unblocks the trigram/exact-layer work (P5), since the same DB hosts both.

**Why reranking is P1, not later.** Cheap given the latency budget, stacks directly on the existing RRF output, and combined with P0 likely captures most of the achievable discovery-quality gain *before* the heavier vector migration.

**Why the vector migration (P3) is high-effort/late despite fixing the headline memory gap.** It is the largest change and is *orthogonal* to everything else (separate file, separate concern). Doing it after P0–P1 means you are not debugging two subsystems at once, and you will have a measurement harness in place to confirm recall is preserved post-quantization.

**Cross-cutting discipline.** Validate every change empirically on a held-out set of real queries from your own repos — because code-retrieval benchmarks (CoIR et al.) are themselves noisy (label noise, tasks that reduce to string matching), the only trustworthy signal is your own corpus.

---

## 5. Key Techniques & References

- **RaBitQ** — Gao & Long, *Quantizing High-Dimensional Vectors with a Theoretical Error Bound for ANN Search* (arXiv:2405.12497); multi-bit extension (2025), GPU version (2026), RaBitQ Library. PQ-superseding quantization with error bounds.
- **DiskANN / Vamana** — Subramanya et al., *Fast Accurate Billion-point NN Search on a Single Node* (NeurIPS 2019). Disk-resident graph ANN. Dynamic: FreshDiskANN; LSM-VEC (arXiv:2505.17152); OOD-DiskANN.
- **FTS5 / BM25** — SQLite FTS5 full-text extension with built-in `bm25()` ranking (`sqlite.org/fts5.html`).
- **Hybrid retrieval / RRF** — sparse+dense fusion via Reciprocal Rank Fusion; SOTA consensus that hybrid beats single-method, especially for identifier-heavy code.
- **Late interaction / ColBERT** — token-level reranking; strong precision. Cross-encoder rerankers (e.g. Voyage rerank-2.5).
- **Code embedding & benchmarks** — `voyage-code-3` (VoyageAI); CoIR benchmark (`github.com/CoIR-team/coir`); CodeXEmbed (arXiv:2411.12644); Qodo-Embed.
- **Trigram code search** — Zoekt / Google Code Search; provably-complete regex candidate generation, verified against live source.

---

*Sequencing in one line: **P0 → P1** first (real BM25 + rerank — biggest quality-per-effort), then **P2** (graph → SQLite), then **P3** (DiskANN/RaBitQ for the memory ceiling), then **P4–P5** (formal correctness + decidable exact-query layer). Validate each step on your own held-out queries.*
