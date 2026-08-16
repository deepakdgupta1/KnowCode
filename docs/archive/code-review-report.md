All 8 deep-dive agents have reported (1 failed on rate-limit, which I re-ran manually). I've independently verified the highest-impact claims. Let me finalize the synthesis.

Here is the deep code review.

---

# KnowCode — Deep Code Review

## Executive summary

KnowCode is a **well-architected, ambitious local-first code-intelligence tool** (~16k LOC of source, 354 passing tests). The layered design (scanner → parsers → graph builder → store → indexer → retrieval → orchestrator → agent) is clean, the optional-extras packaging is thoughtful, and the security-conscious "fail-closed" routing policy is more rigorous than most projects of this size.

However, this review surfaced **a set of issues that together undermine the project's core value propositions** in ways the test suite does not catch:

1. **CI is currently red on `main`** — the latest commit (`bdd27de`) introduced duplicate function definitions that break both `ruff` and `mypy` (which isn't even run in CI). A fail-loud contract is silently broken at runtime.
2. **The language parsers — the product's foundation — have severe correctness gaps.** TypeScript drops *all exported declarations*; JavaScript never captures class inheritance; Vue reports every entity on one line; Rust `impl` edges dangle. These are empirically verified against the tree-sitter grammars.
3. **The hybrid search default (`hybrid_alpha`) disagrees with itself in three places**, silently running sparse-dominated (80/20) instead of the documented equal-weight (50/50) blend.
4. **Concurrency is unsafe** under `--watch` + server load: data loss on embed failure, non-atomic JSON persistence, a rate limiter that's bypassable, and store-level races.
5. **Two genuine security findings**: prompt injection (system instructions in the user turn) and unredacted user queries persisted to plaintext telemetry.

What follows is prioritized. Items marked **✅ verified** I personally confirmed by reading the code or running the tooling; others come from the systematic subsystem reviews and are cited to `file:line`.

---

## 🔴 Critical issues

### C1. CI is broken on `main` right now ✅ verified
`src/knowcode/llm/embedding.py:227` and `:300` both define `resolve_embedding_dimension`; `:237` and `:319` both define `build_provider_from_model`. Introduced in commit `bdd27de`.
- `ruff check .` (the **exact** CI command) fails with 2× `F811`.
- The two `resolve_embedding_dimension` impls **diverge**: L227 raises `ValueError` for unknown providers (fail-loud); L300 silently returns `1024`. The second shadows the first at runtime, so **the fail-loud contract is silently dead** — a misspelled provider string now silently yields a 1024-dim config.
- `mypy --strict` fails too — but **mypy is not in CI at all** (`.github/workflows/ci-cd.yml` runs only `ruff` + `pytest`). The CI matrix also still lists Python 3.9 while `requires-python = ">=3.10"`.
- **Fix:** delete the L227–269 duplicates; keep the fail-loud behavior (raise on unknown provider); add a mypy step to CI; drop 3.9 from the matrix.

### C2. Language parsers silently lose large classes of entities ✅ (grammar-verified by the parser review)
These are the highest-impact correctness defects in the project — they mean the knowledge graph is missing exactly the things users query most:

| Bug | File:line | Impact |
|---|---|---|
| **TypeScript drops all `export`ed declarations** | `typescript_parser.py:41-97` | The TS override reimplements the child loop and omits the `export_statement` branch the JS parent has. In normal TS (where nearly everything is exported), **most entities are silently dropped.** |
| **JS class inheritance never captured** | `javascript_parser.py:182-194` | Reads `super_class` field off `class_heritage`, which the grammar doesn't expose (verified). Every JS `INHERITS` edge is lost. |
| **Vue line numbers uniformly wrong** | `vue_parser.py:285-506` | Every prop/method/computed gets `line_start=line_offset, line_end=line_offset+1` (the `<script>` block's first line). Jump-to-definition is broken for the whole Vue script body. |
| **Vue `<template v-if=...>` dropped entirely** | `vue_parser.py:186-190` | The template-opener regex `r"<template\s*>"` requires the tag to close immediately. Any templated attribute → entire template tree (the "MOST IMPORTANT relationship" per the code's own comment) lost. |
| **Rust `impl` edges dangle** | `rust_parser.py:329-373` | Edges use `source_id="type::Foo"` / `target_id="trait::Bar"`, but no entity is ever created with those IDs. Every `IMPLEMENTS` edge + type-scope `CONTAINS` never resolves. |
| **Python: no nested classes/functions, no decorators, no module vars** | `python_parser.py:98-181` | `iter_child_nodes` only handles top-level `ClassDef`/`FunctionDef`; `_parse_class` doesn't recurse into nested classes; decorators never recorded. |
| **Java: drops enums, records, `implements`, fields, nested types** | `java_parser.py:34-138` | Only `class_declaration`/`interface_declaration` + `method_declaration` handled; the `interfaces` field is never read. |

**These exist because the parser test suite only checks the "missing/empty/valid contract," not that real constructs are extracted.** Fixing this is the single highest-value work.

### C3. The documented hybrid-search default is not what runs ✅ verified
`hybrid_alpha` (the *dense* weight in the RRF blend) has three different defaults:
- `AppConfig.hybrid_alpha = 0.2` (dataclass field, `config.py:64`) and `AppConfig.default()` (`config.py:154`)
- `AppConfig._load_from_yaml` fallback `0.5` when the key is absent (`config.py:270`)
- `HybridIndex.__init__` default `0.5` (`hybrid_index.py:16`)
- Architecture docs (`docs/research/knowcode-architecture-synthesis.md`) say α=0.5 = "equal weight."

`service.py:197` wires `self.app_config.hybrid_alpha` → so the normal runtime path runs **α=0.2 = sparse 80% / dense 20%**. Dense (semantic) retrieval is nearly silenced versus what the docs describe. The RRF math itself is **correct** (K=60, proper rank weighting — I read `hybrid_index.py:61-65`); only the default is wrong. Also: no `0.0 ≤ α ≤ 1.0` range check in YAML loading.

### C4. Background indexer loses data on embedding failure
`indexing/background_indexer.py:79-99` calls `remove_file(args)` **before** `index_file(args)`. If `index_file` throws (API timeout, rate limit, parse error), the broad `except Exception` logs and moves on — but the old chunks are already deleted. The file ends with **zero chunks**, silently. No retry, no rollback. On the live `--watch` path this corrupts the index on any transient embed failure.

### C5. LanceDB filter-string injection
`storage/lancedb_vector_store.py:82,145` string-interpolate `chunk_id` into SQL-like predicates:
```python
self._table.search().where(f"id = '{chunk_id}'")
self._table.delete(f"id = '{chunk_id}'")
```
`chunk_id` derives from `entity_id` = `file_path::qualified_name::index`. A path/qualified name containing a quote (legal on most filesystems and in many languages) breaks the query, and attacker-controlled input can widen the match — including for `delete`, which could wipe the table. **Fix:** validate `chunk_id` against `^[A-Za-z0-9_./:@-]+$`, or use LanceDB's parameterized `where`.

### C6. Knowledge-store JSON save is non-atomic
`storage/knowledge_store.py:86-88` truncates and rewrites in place with no `fsync` and no temp-file + `os.replace`. A crash/OOM/power loss mid-write — or an interrupted `--watch` re-index — leaves a corrupt `knowcode_knowledge.json`. **Fix:** write to `.json.tmp`, flush+fsync, `os.replace`.

### C7. FAISS `remove()` corrupts the index and silently under-retieves
`storage/vector_store.py:201-205` only deletes the id-map entry; `IndexFlatIP` can't remove vectors. The dead vector stays in FAISS, occupies top-k slots, and is filtered at search time — so callers silently receive **fewer than `limit` results** even when good matches exist further down. `count()` and `index.ntotal` diverge. Hit on every re-index (indexer does remove-then-re-add). **Fix:** rebuild from `id_map`, or use `IndexIDMap2`, or make `remove` rebuild-on-demand.

### C8. Prompt injection: system instructions live in the user turn
`llm/agent.py:108,144-150` concatenates `system_instructions + context_str + query` into a single `{"role": "user"}` message. Retrieved codebase content (which may contain hostile comments) and the user query have equal standing with the "system" instructions, and OpenAI/OpenRouter instruction-hierarchy protections don't apply outside the `system` role. **Fix:** send instructions as a `system` message; fence `context_str` and label it untrusted data.

### C9. Raw user queries persisted to plaintext telemetry
`llm/agent.py:301-315` and `service.py:273-282` log `"query": query` verbatim. `telemetry.py:32-33` writes `json.dumps(event)` to `knowcode_telemetry.jsonl` with no redaction or length cap. A user pasting a key/token/PII into a query persists it to disk indefinitely. **Fix:** redact common secret patterns (`sk-`, `AIza`, `Bearer `, hex tokens) and truncate (e.g., 200 chars) before logging.

### C10. Rate limiting is trivially bypassable ✅ verified the decorator layer is present; the keying is the flaw
`api/rate_limit.py:20` uses slowapi's `get_remote_address`, which reads `X-Forwarded-For` first. For a local-first server with no trusted reverse proxy, any client can send a unique spoofed `X-Forwarded-For` per request and get a fresh bucket — the 60/min and 10/min limits never trigger. Combined with unbounded `/trace_calls`/`/impact` traversal on the JSON backend (`knowledge_store.py:353-420`, O(results · R) per request), this is a cheap local DoS surface. **Fix:** `key_func=lambda req: req.client.host or "unknown"`.

### C11. Indexing correctness: scanner + monitor don't honor gitignore correctly
- `indexing/scanner.py:41` — `respect_gitignore` flag is stored but **never read**; gitignore is always applied. Callers passing `False` get the opposite behavior.
- `indexing/scanner.py:47` — only the root `.gitignore` is loaded; **nested `.gitignore` files are ignored**, so files excluded in subdirectories (scratch dirs, secrets committed-then-ignored, test fixtures) are indexed.
- `indexing/monitor.py:103-109` — the live monitor checks only the file extension, never whether the path is ignored. Editing `.venv/.../numpy/__init__.py` triggers re-indexing.

---

## 🟠 High-severity issues (grouped)

**Retrieval correctness**
- `retrieval/search_engine.py:85-107` — a chunk that is both a primary result *and* a callee of an earlier primary gets mislabeled as a zero-score "dependency" and then **excluded from entity selection** by `orchestrator.py:133`. Genuine retrieved entities silently vanish. (Pre-seed `seen_ids` with primary IDs, or expand in a second pass.)
- `retrieval/orchestrator.py:118-181` — no lexical fallback when semantic search *succeeds but returns empty* (fallback only triggers on exception). Empty index → empty answer, no fallback.
- `retrieval/orchestrator.py:112,186` — per-entity token budgets are not enforced against the aggregate `max_tokens`; the response can advertise `total_tokens > max_tokens`.
- `config.py:274-276` — `reranker_top_k_multiplier` is validated as `int` but not as positive (and `bool` isn't excluded). `multiplier=0` → `limit*3=0` → empty results → silent total failure.

**Storage integrity & thread safety**
- `storage/sqlite_knowledge_store.py` / `sqlite_chunk_repository.py` — **no schema versioning is persisted to the DB** (`SCHEMA_VERSION=1` constants are never written or read). `doctor` even reports the constant as if it reflected on-disk state. Old DB + new binary silently reuses a stale schema.
- Relationships are inserted with plain `INSERT` and no `UNIQUE(source_id,target_id,kind)` — every incremental re-index appends duplicate edges; `get_callers`/`get_callees` lack `DISTINCT` so duplicates surface (inconsistent with `get_dependencies` which has it).
- Both SQLite stores use **one shared connection** with `check_same_thread=False` and a lock only around writes; reads take no lock. With `--watch` (background writer) + concurrent request handlers, this is a live race.
- `storage/sqlite_chunk_repository.py:360-378` — `load()` swaps the DB path and reconnects but never calls `_init_schema()`; pointing at a fresh DB → subsequent inserts fail. The docstring also advertises a legacy migration that doesn't exist.

**Indexing performance & correctness**
- `indexing/indexer.py:62-67` — `index_directory` calls `builder.build_from_directory(root)` (full parse, results discarded), then re-scans, then re-parses each file. **Every file is parsed 2-3×.**
- `indexing/indexer.py:350-357` — `remove_file` does `clear()` + re-add **every vector** on each deletion → O(N) per changed file; K edits = O(N·K). `vector_store.remove(id)` already exists.
- `indexing/monitor.py:64-72` — no debounce; watchdog emits 3-10 events per save, each queuing a full remove+re-index+embed.
- `indexing/chunker.py:209` — `overlap >= max_chunk_size` → `start += (max-overlap) ≤ 0` → **infinite loop / OOM**. No validation.
- `indexing/indexer.py:136-148` — incremental index diffs committed-vs-committed; **uncommitted working-tree edits aren't detected**, and git-relative paths are joined onto a possibly-subdirectory `root_path` producing wrong absolute paths.
- `indexing/graph_builder.py:169-181` — `_find_entity_by_name` returns the first match (dict insertion order, non-deterministic across scan orders); two classes named `Parser` in different modules both resolve to whichever loaded first → silent false-positive `calls`/`inherits` edges. No ambiguity detection.

**LLM**
- `llm/agent.py:157-164` — server-side rate-limit detection string-matches `e.__class__.__name__ == "ResourceExhausted"`. OpenAI raises `RateLimitError` (never matched); SDK renames or subclasses break it silently. Catch concrete types + HTTP 429.
- `llm/embedding.py:90-173` + `storage/vector_store.py:90` — **no embedding-dimension validation anywhere.** A wrong/default dim from C1 produces a cryptic FAISS error deep in `add`/`search` instead of a clear config error.
- `llm/rate_limiter.py:24-47,90-96` — `_load → mutate → _save` with no file locking and non-atomic writes; concurrent CLI + MCP server (sharing `~/.knowcode/usage_stats.json`) lose records. Token-based limits (`tokens_free_tier_limit`) are **never enforced** — only RPM/RPD.
- `llm/agent.py:60-65` — `mistralai` is force-routed through OpenRouter (breaks real Mistral keys); `glm`/`z-ai` with `GLM_BASE_URL` unset → `base_url=None` → the OpenAI client silently targets `api.openai.com` with a GLM key → opaque auth failure.

**API / MCP**
- `api/main.py:56`, `cli/cli.py:584` — bind defaults to `127.0.0.1` (good ✅), but `--host 0.0.0.0` is accepted with no warning. Every endpoint returns raw source code + absolute paths + the whole call graph with **no auth**. Non-loopback binding is a silent footgun.
- `api/api.py:287-297` — `/reload` is unauthenticated; its catch-all 500 handler returns `str(e)`, routinely leaking absolute filesystem paths.
- `service.py` — no `threading.Lock` anywhere; `reload()` nulls `_store` while concurrent readers iterate `store.entities`.
- `api/api.py:71-82,234` — no `max_length` on `query`/`q`. A 10 MB query gets substring-scanned over every entity (JSON backend) and/or POSTed to the embeddings API.
- `service.py:286-335` — `get_freshness_metadata()` does a **full source-tree walk on every call**, and `retrieve_context_for_query` always calls it → every retrieval pays for a full filesystem walk.
- `mcp/server.py:351-370` — MCP integer args (`max_tokens`, `limit_entities`) aren't clamped to the schema-advertised maxima.

---

## 🟡 Medium / design observations

- **`hybrid_alpha` is not the only config inconsistency.** `doctor._check_dependencies` (doctor.py:284-316) warns about "FAISS / MockVectorStore fallback" but the default backend is now **lancedb** — the check is stale.
- **`KnowCodeService` is a ~700-line god object** (`service.py`): store loading, index build/cache, embedding wiring, retrieval, freshness, full analyze pipeline, stats. `store` is typed `Any`; backend differences are papered over with `hasattr(...)`/`isinstance(...)` branches. The dead-branch bug in `get_stats` (storage review H1) is a direct symptom: the optimized `GROUP BY` path is unreachable because both store types expose `.entities`.
- **Leaky abstractions:** `service.py:482-491` and `service.py:632,637` reach into `sqlite_store._conn` (private) and run raw SQL — including manual `BEGIN`/`COMMIT` outside the store's own lock. Stats and bulk-write belong on the store.
- **`Entity.__eq__/__hash__` use only `id`** (`data_models.py:94-100`). Two entities with the same id but different fields are "equal," and putting them in a set/dict silently drops one. This already bites the parsers (duplicate Markdown/RST heading titles collide on id → second section lost).
- **Three vector-store files with duplicated schema constants** (`vector_store.py`, `lancedb_vector_store.py`, `vector_backends.py`) that can drift; `LanceDBVectorStore` never declares `VectorStoreProtocol` conformance.
- **Telemetry module-level `ThreadPoolExecutor`** (`telemetry.py:9`) is never shut down (no `atexit`), and after interpreter-shutdown begins, submitted tasks raise `RuntimeError`. Also has no queue bound.
- **Reranker writes telemetry to CWD** (`reranker.py:109` passes `"."`) — pollutes the invocation directory and is invisible to `get_telemetry_summary(store_path)`.
- **Pervasive `print()` in library code** (`agent.py` ~12 sites, `rate_limiter.py`, `reranker.py`) — corrupts structured/MCP stdout; should be `logging`.
- **`aimodels.yaml` is committed** with rate-limit overrides (`rpm_free_tier_limit: 99999`, `tokens_free_tier_limit: 200000000`) that effectively **disable** the free-tier rate limiter for this deployment. The README also still references `OPENAI_API_KEY` / `GOOGLE_API_KEY_1` while `.env.example` has moved to `GLM_API_KEY` / `GOOGLE_API_KEY` — doc drift.
- **Many `__init__.py` files are empty** (llm, storage, indexing, analysis), so the intended public API is implicit and callers use deep imports.

---

## Test-suite assessment

The suite is **green (354 passed)** but its coverage of *correctness* (vs. structure) is the project's weakest link:

- **Parsers** are tested only for the "missing/empty/valid contract," never that real constructs (TS exports, JS inheritance, nested classes, decorators) are actually extracted. This is *why* C2 went undetected.
- **`query_classifier.py` (154 lines, feeds the security-sensitive routing gate) has zero test references.**
- **Rate limiting** is referenced in tests only to *disable* it (autouse fixtures); enforcement is untested.
- **`routing_policy.py`** is well tested at the top level, but the path-traversal guard (`_repository_path`), the judges/dataset/evidence/metric-floor validators, and the threshold bounds have **no direct tests** — the tampering surface is larger than the tests imply.
- **Concurrency** coverage is read-only (5 reader threads, no concurrent writes) — the actual WAL/lock risks are untested.
- **Two "manual" tests** (`test_failover_manual.py`, `test_rate_limiter_manual.py`) *are* collected by default pytest (despite the name) and assert almost nothing — the `_manual` suffix is misleading.
- The `@pytest.mark.eval` marker is defined but unused; the unused mypy/3.9-matrix CI config lets type regressions (like C1) merge.

**Strong spots:** routing-policy fail-closed invariants, strict-config loading (11 parametrized cases), the `retrieve_context_for_query` projection tests, and the real-git incremental-indexer integration test.

---

## What's done well

- **Clean layered architecture** and a real protocol layer (`protocols.py`) that decouples retrieval from storage.
- **The routing-policy security model is genuinely fail-closed** at the top: `_fail_closed` clears `local_answer_task_types` after every YAML load; local answering requires both a checksum-pinned artifact *and* a sufficiency score above threshold; the query classifier is deterministic regex (not LLM), so task type can't be hallucinated into the allowlist.
- **No secrets in tracked files; no unsafe deserialization** (`json.load` everywhere; no `pickle`/`eval`/`yaml.load`/`shell=True`). ✅ verified
- **`.gitignore` correctly excludes** `.env`, `knowcode_knowledge.json`, telemetry, `.db` files.
- **Pydantic bounds** on the HTTP layer (`limit` 1-50, `max_tokens` 100-8000, `depth` 1-5) are correctly enforced; **no CORS middleware** (correct for a local tool).
- **RRF math is correct** (K=60, proper alpha-weighted rank fusion) — only the default is wrong.
- SQLite injection surface is clean (all `?`-parameterized); FTS5 token sanitization is sound.

---

## Recommended priority order

1. **Fix CI now** (C1): delete the duplicate defs, restore fail-loud behavior, add mypy to CI, drop 3.9. *Blocks all other merges from being trustworthy.*
2. **Parser correctness sprint** (C2): add tests that assert real constructs are extracted across all 9 parsers, then fix the TS-exports / JS-inheritance / Vue-line-numbers / Rust-impl-edges bugs. *This is the product's core value.*
3. **Pick one `hybrid_alpha` default** (C3) and add a range check — one-line change with real retrieval-quality impact.
4. **Stop the silent data-loss paths**: reverse remove-before-index (C4), make JSON save atomic (C6), fix FAISS `remove` (C7).
5. **Security hardening**: parameterize LanceDB filters (C5), move instructions to the `system` role (C8), redact telemetry (C9), fix the rate-limiter keying (C10).
6. **Concurrency**: add a lock around `KnowCodeService` lazy-load + reload; fix SQLite single-connection sharing under `--watch`.
7. **Scanner/monitor gitignore correctness** (C11) — single source of truth for "is this path ignored," used by both scan and live-event paths.
8. **Then** the medium items: stale doctor checks, `KnowCodeService` decomposition, store-level stats APIs, telemetry-executor lifecycle, logging vs print.

The architecture is sound and the security *design* is unusually careful for this stage — but the gap between what the docs/CI imply is verified and what the parsers/storage actually do under load is the central risk. Closing the parser-correctness and CI gaps would move this from "promising but unsafe to depend on" to genuinely reliable.