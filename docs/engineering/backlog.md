# Engineering Backlog

Known defects and deferred work that no current workstream owns. This exists so
that something found while doing other work is not lost when that work ships.

The [roadmap](../roadmap.md) says what the project is building next. This says
what it already knows is wrong. An item leaves here when it is fixed, when a
roadmap workstream adopts it and the row points at that workstream, or when it
is rejected outright. A rejected item still gets a Closed row, because a
measured reason not to build something is worth more than silence.

## Conventions

- One row per item, newest first within a severity.
- **Severity** is the user-visible consequence, not the effort. `Critical` means
  wrong or missing answers. `High` means a real defect with a workaround.
  `Medium` and `Low` are cost, clarity, and hygiene.
- Every item names how it was found and how to reproduce it. An item nobody can
  reproduce is a rumour.
- An item deferred on purpose says why, so the next reader does not re-litigate
  the decision.
- Ids are allocated once and never reused, including by a closed item. Check
  both sections before taking the next number — concurrent sessions have
  collided on one already (BL-17).
- Nothing here may cost search or retrieval quality. An item whose fix trades
  recall, precision, or exactness for footprint or speed is rejected, not
  deferred, however small the measured loss. This is
  [DR-4](../research/storage_optimization_2026_v4.md) in the storage plan; the
  two documents state one rule.

## Open

### BL-15 - The exact search mode treats `_` and `%` as wildcards

**Severity:** High. **Found:** 2026-08-30, measuring what a replacement exact
plane would have to offer for BL-14.

`SqliteChunkRepository.search_exact` interpolates the caller's string straight
into a `LIKE` pattern with no `ESCAPE` clause:

```python
f"SELECT {_SELECT_COLUMNS} FROM chunks WHERE content LIKE ? LIMIT ?",
(f"%{pattern}%", limit),
```

In SQLite `LIKE`, `_` matches any single character and `%` matches any run.
Both are ordinary characters in source code, and `_` is in most Python and Rust
identifiers. So the mode named `exact` silently answers a different query than
the one asked, and `ExactQueryEngine.search_scored` scores every row it returns
`1.0`, which says the match was exact.

Measured against generation `20260830T061050304877Z-8abc9f11`, unbounded:

| Quoted query | Rows served | Rows containing the literal | False |
|---|---:|---:|---:|
| `"vector_"` | 927 | 278 | **649, 70%** |
| `"HOSTILE_CONTEX"` | 17 | 16 | 1 |
| `"search_ex"` | 6 | 6 | 0 |

Over 74 sampled queries containing `_` or `%`, 11 were answered differently
from their literal reading. The size of the error tracks how common the
neighbouring characters are, so it is largest exactly where the identifier is
generic.

`LIKE` is also case-insensitive over ASCII by default, which the mode does not
state either. That one may well be wanted; it just is not written down
anywhere.

Reproduce:

```python
from knowcode.retrieval.exact_query_engine import ExactQueryEngine
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

engine = ExactQueryEngine(SqliteChunkRepository(GEN / "chunks.db"))
len(engine.search_scored('"vector_"', limit=10000))
```

Compare against `content LIKE '%vector\_%' ESCAPE '\'` on the same database.

**Not fixed here on purpose, and no longer waiting on anything.** It was
deferred to the decision BL-14 said had to be made about what semantics the
exact plane offers. That decision is made: BL-14 and Phase F are rejected, the
`content` column stays, and `LIKE` stays the exact plane's implementation. So
the question this item was waiting on has an answer, and the answer is that
`exact` means the literal string.

The change is one `ESCAPE` clause and an escape of `%`, `_` and the escape
character in the pattern. Write down the two semantics the mode then offers —
literal substring, ASCII-case-insensitive — and pin them with a test that
asserts a `_` in the query does not match a different character. It narrows
what a quoted query returns, which is the point: every row it stops serving is
one that does not contain what was asked for.

### BL-9 - A symbol named after its own file shadows the file's module entity

**Severity:** High. **Found:** 2026-08-29, writing the parser-contract
uniqueness assertion that closed BL-1.

Every parser gives a file's MODULE entity the file stem as its qualified name.
A top-level symbol whose name is also the stem therefore produces the same
entity id, and the two entities silently overwrite each other in
`knowledge.db`. This is BL-1's defect in a second place: identity derived
without lexical scope. ADR 1's own example, `module.Type.method`, says a
module-scoped symbol should carry the module prefix, and none does.

Unlike BL-1 the file is not rejected, because the chunker skips MODULE
entities and so emits no duplicate chunk id. What breaks is the graph.

Measured on this repository: **3 of 313 indexable tracked files**, one of them
`src/knowcode/cli/cli.py`, where the MODULE entity spanning lines 1-1071 and
the `cli()` function at lines 23-27 share
`src/knowcode/cli/cli.py::cli`, and **48 relationships point at the ambiguous
id**. The others are `scripts/fix_swallowed.py` and
`tests/integration/test_golden_queries.py`.

It is language-independent. Confirmed for Python, JavaScript, and TypeScript,
and the mechanism holds for every parser that emits a MODULE entity. It is
commoner in JavaScript and TypeScript, where `format.ts` exporting
`function format()` is ordinary style.

Reproduce:

```bash
pytest tests/unit/parsers/test_parser_contract.py -k named_after_its_file -rx
```

That test is a strict `xfail` carrying this item's id, so it fails loudly the
day the defect is fixed and the marker has to come off.

**Deferred on purpose.** The honest fix is to give module-scoped symbols the
scope prefix ADR 1 already describes, which changes every entity id in the
index and touches all nine parsers. Do not paper over it with a reserved
`::module` token; ADR 1 rules out ad hoc internal namespaces, and BL-6 is the
evidence that inventing one produces ids nothing else agrees with.

**This item no longer has a carrier.** It was deferred to Phase C on the
premise that Phase C rewrites id encoding anyway. C1 shipped instead as a
storage-layer codec (ADR 0010): ids are unchanged above the store, and only
their stored form is relative. Nothing in Phase C touches how a qualified name
is built, so BL-9 now costs what it costs on its own. Size it as a parser
change, not as a rider on storage work.

### BL-10 - A Vue file has no entity standing for the file itself

**Severity:** Low. **Found:** 2026-08-29, closing BL-6 in Phase B.

Every other code parser emits a MODULE entity for the file, and the prose and
YAML parsers emit a DOCUMENT one. `VueParser` emits neither. Its entities are
the components, functions and variables inside the single-file component, and
nothing represents the file.

Phase B made the module-header and imports chunks hang on that entity. With no
entity to hang on they are now skipped, so a Vue file's top-of-file text is not
indexed. The alternative was to keep emitting them against a synthetic
`<file>::module` id, which is the defect BL-6 was.

Measured on this repository: **854 bytes across 5 files**, all of them parser
test fixtures. There is no production Vue in this tree, which is why this is
Low rather than High.

Reproduce:

```bash
python - <<'EOF'
from knowcode.data_models import EntityKind
from knowcode.parsers.vue_parser import VueParser
kinds = {e.kind for e in VueParser().parse_file(
    "tests/fixtures/mixed_language/widget.vue").entities}
print(EntityKind.MODULE in kinds or EntityKind.DOCUMENT in kinds)
EOF
```

The fix is a MODULE entity in `VueParser` covering the whole file, matching
what the other code parsers already do. Watch for BL-9 while doing it: naming
that entity after the file stem is what makes a top-level symbol collide with
it.

**Deferred on purpose.** It affects no production file in this repository, and
it is a parser change with its own contract tests rather than part of the
chunking work that surfaced it.

### BL-11 - A chunk is content-addressed by MD5

**Severity:** Medium. **Found:** 2026-08-29, executing Phase C5.

`Chunker` hashes chunk content with `hashlib.md5` in all four places it mints a
`content_hash`, at `chunker.py:197`, `:244`, `:368` and `:393`. Entities use
SHA-256 through `compute_entity_content_hash`, so the two halves of one index
are addressed by digests of different strength.

That hash is not decorative. `Indexer._reuse_durable_embeddings` looks a chunk
up by it and, on a hit, attaches the stored embedding to the new chunk. A
collision therefore hands one chunk another chunk's vector, and the chunk still
retrieves, so nothing fails loudly. MD5 collisions are cheap to construct, which
makes this reachable by a crafted source file rather than by chance alone.

`ProseChunker` already computes SHA-256 for `ProseChunk.content_hash`
(`prose_chunker.py:310`, `:346`), and `chunker.py:197` throws it away and
re-hashes the same bytes with MD5. So the stronger digest is computed and
discarded on the prose route.

Reproduce:

```bash
sqlite3 <generation>/chunks.db "SELECT DISTINCT LENGTH(content_hash) FROM chunks"
```

That returns 16, the packed width of a 32-character MD5 digest. An entity digest
in the same generation is 32 bytes.

**Deferred on purpose.** Changing the digest changes every chunk's reuse key, so
every generation must rebuild. Phase C already bumped `Indexer.SCHEMA_VERSION`
once for C5, and Phase E revisits embedding policy, which is the same reuse
path. Doing it there costs one rebuild instead of two. `pack_content_hash`
already packs both widths, so the storage layer needs no further change.

### BL-7 - Timing-sensitive tests flake under concurrent load

**Severity:** Low, but it costs trust in the suite. **Found:** 2026-08-29, while
two agent sessions ran the suite on the same machine.

Two tests fail intermittently in a full-suite run and never in isolation, and it
is a different test each time:

- `tests/unit/indexing/test_index_batching.py::test_a_bulk_build_retries_a_transient_provider_failure`
  waits on a thread barrier with `timeout=5.0` (`test_index_batching.py:96`).
- `tests/integration/test_generation_hotswap.py::test_the_api_serves_requests_while_reload_runs`
  waits on events and thread joins with 30 and 60 second timeouts.

Measured: 1 failure in 4 full-suite runs, different test each time, 0 failures in
5 runs of the module in isolation and 3 runs of the single test. Load average was
2.9 with two agent sessions running suites concurrently.

The suspected cause is wall-clock waits sized for an idle machine, not a
correctness defect: nothing in the product changed between a passing and a
failing run. That is a hypothesis, not a finding. Confirm it by running the
suite under deliberate CPU load before changing any timeout.

Do not simply raise the timeouts. A barrier that needs more than five seconds
for threads that are already spawned is worth understanding first, and a longer
timeout converts a fast failure into a slow one.

**Deferred on purpose.** It is test infrastructure, not product behaviour, and
it was found while diagnosing something else.

### BL-8 - A generation manifest cannot witness row loss in either database

**Severity:** Medium. **Found:** 2026-08-29, mutation-probing Phase A2.

`build_manifest` byte-digests only `index_manifest.json`. `chunks.db` and
`knowledge.db` are described by logical id digests and counts, and
`read_chunk_ids`, `count_durable_embeddings`, and `_assert_chunk_vector_parity`
all read them out of the staged file at the end of the build. Every number in
the manifest therefore derives from the artifact it is meant to check.

Reproduce it by making `SqliteChunkRepository.compact` drop one row before it
vacuums. The build publishes, and `validate_generation(..., verify_digests=True)`
returns no failures, because the manifest recorded the post-deletion id list.
Delete `WHERE rowid = (SELECT MIN(rowid) FROM chunks)` rather than every
seventh row. A modulo probe deletes nothing on the small corpora the watch
tests build, which reads as a clean run and is a false negative.

Independently reproduced 2026-08-29 by a second session, on a scratch copy with
the probe applied: a generation missing **1,003 of 7,025 chunks, 14% of the
corpus, published successfully** and `validate_generation(..., verify_digests=True)`
returned no failures. The manifest recorded 6,022 for both `counts.chunks` and
`counts.vectors`. The D1 durable-embedding guard shares the blind spot for the
same reason, and [ADR 9](adr/adr-0009-derived-vector-plane.md) now says so.

A trap for anyone repeating this: the repository venv installs `knowcode`
editable against `src/`, so running a scratch copy with the venv's interpreter
executes the *shared* tree and the probe appears to do nothing. Set
`PYTHONPATH=<scratch>/src`, and assert the loaded `__file__` is the scratch one
before trusting a negative result.

This was theoretical until Phase A2, which introduced the first step that
rewrites an artifact between indexing and publication. All three publication
paths now carry a witness taken before the rewrite.
`test_compaction_does_not_lose_rows_between_indexing_and_publication` compares
the published chunk count against the count `index_directory` reported, and
`test_a_watch_publication_compacts_without_losing_chunks` compares it against
the staged repository's own row count read before `publish`. Both fail on a
single lost row, as do the store-level tests that compare raw row bytes.

**Rejected fix: threading the indexer's chunk count into
`_assert_chunk_vector_parity`.** Proposed as a two-line change that would catch
every build-time loss rather than compaction's alone. It does not, because the
count it would thread is only a corpus total on one of the three paths.
`index_incremental` returns the chunks it touched in changed files, not the
total, and the watch path has no total at all: it commits per-file
transactions and never counts the corpus. A guard that holds on the full
rebuild and silently means something else on the other two is worse than the
three explicit tests.

**Deferred on purpose.** Compaction is the only step that rewrites a staged
database, and all three of its paths are covered. Adding the two databases to
`artifact_names` is not the fix either; that digest would be computed from the
damaged file for the same reason the counts are. Closing this needs a witness
that survives every path, and the honest place to design one is Phase C, which
is the next change that rewrites these files after the counts are taken.

### BL-5 - The storage plan's code anchors are stale

**Severity:** Low. **Found:** 2026-08-29, executing Phase D1.

`docs/research/storage_optimization_2026_v4.md` §5 and §14 cite line numbers
that have drifted by roughly 250 lines. `_recover_vectors_from_chunks` is at
`indexer.py:457`, not `:381`, and `Indexer.save` is at `:836`, not `:581`. §17
records the corrections but the anchors themselves were left as written.

The same section also documents that the generation the plan measured no longer
exists, so §3's three reproduction commands need a rebuild first, and that
`_recover_vectors_from_chunks` is scoped to one file transaction rather than the
whole corpus as §5 implies.

**Deferred on purpose.** Line anchors go stale on every commit. Fix them only if
the anchors are re-verified as part of adopting a later phase.

### BL-23 - Freshness cannot see a deleted file

**Severity:** Medium. **Found:** 2026-08-30, same audit.

`get_freshness_metadata` derives staleness from

```python
latest_source_change = max(os.path.getmtime(f.path) for f in files)   # service.py:685
```

— the maximum mtime across files that *currently exist*. Deleting a source file
raises no file's mtime, so `is_stale` stays `false` while the index keeps
serving entities for a file that is gone. No file count or path set is
compared, so nothing else can catch it either.

The spec does call freshness "mtime-based and advisory", which covers the shape
of this in spirit; the specific deletion blind spot is worth naming because the
staleness banner is what tells a user their answer may be wrong.

Partly backstopped by D3: under the default `entity_source: disk`,
`LiveSourceLoader.load_verified_source` fails closed on a missing file, so no
stale *body* is served for a deleted file — only its name, signature and
relationships. Under `entity_source: stored` the body is served too.

Fix by recording the indexed file count in the manifest and comparing it
against the scan's count; a drop means a deletion. That is O(1) and
proportionate to an advisory signal. It misses a same-count delete-plus-add,
but the added file's mtime catches that case already.

Separately, this runs `scanner.scan_all()` plus one `stat` per file on the
retrieval path, on every call. Worth measuring before it is worth changing.

### BL-24 - Every context bundle labels its source as Python

**Severity:** Low. **Found:** 2026-08-30, same audit.

All four signature and source-code fences in `ContextSynthesizer` are hardcoded
to ` ```python ` (`context_synthesizer.py:177`, `:202`, `:472`, `:480`), so
Rust, TypeScript, Java and Vue source reaches the LLM — and the user — declared
as Python. Wrong syntax highlighting for the reader, and a false signal to the
model about what it is being asked to reason over.

The language is already on `entity.metadata["language"]`, and
`documentation_synthesizer.py:434` already reads it. Extract the fence-tag
lookup once and use it in both places rather than writing the mapping twice.

## Closed

| Item | Resolution |
| --- | --- |
| Module chunks point at an entity that does not exist (BL-6) | Fixed 2026-08-29. Module-header and imports chunks hang on the file's MODULE entity, or its DOCUMENT entity where that is what the parser emits; prose chunks hang on the section whose span their lines fall in. Paired build: 495,985 chunk bytes off the graph to 0. `no chunk points at an entity that does not exist` is now a chunker contract for seven languages. Left BL-10. |
| Prose chunked by a Python header extractor, 39% unreachable | Phase B, 2026-08-29. `.md` and `.rst` route to `ProseChunker`. Prose coverage 41.6% to 95.7% over 55 files; the largest document went from 13% to 99%. |
| Class bodies stored twice, and label-only chunks (6.1, 6.3) | Phase B, 2026-08-29. A class chunk stops at its first member; an entity with nothing but a name emits no chunk. 881,970 bytes of duplicated member text and 68 config-key chunks removed, worth 8.00 MB of `chunks.db`. |
| Markdown documents dropped by an entity id collision (BL-1) | Fixed 2026-08-29. A section's qualified name now carries its heading path, so it cannot collide with its own document, and sibling headings sharing a title take an ordinal. Paired build over 55 markdown files: 17 rejected files to 0, prose bytes indexed 357,114 to 509,046. Entity id uniqueness is now asserted for all nine parsers, which is what found BL-9. |
| `knowledge.db` carried unmeasured free-page slack (BL-4) | Phase A2, 2026-08-29. Both databases are vacuumed on the staged copy before the generation is digested. Measured at 3.79 MB over one corpus, against the plan's 2.03 MB estimate; nearly all of it is B-tree repacking rather than free pages. |
| Every embedding stored twice, 28.5% of a generation | Phase D1, 2026-08-29. [ADR 9](adr/adr-0009-derived-vector-plane.md). |
| BL-2, a full rebuild reverting a concurrent watch publication | Fixed 2026-08-29. `build_generation` compare-and-swaps on `expect_current` and re-derives on the generation published in between, bounded by `_REBUILD_REBASE_ATTEMPTS`. The flaky convergence test went from 13 failures in 40 runs to 0 in 40. |
| Publication asserted the presence of a cache and compared two manifest numbers to check chunk/vector membership | Phase D1, 2026-08-29. The guard now counts durable embeddings in `chunks.db`, which is what [ADR 3](adr/adr-0003-durable-embedding-representation.md) already required. |
| Phase D2's contentless FTS table cannot delete a chunk (BL-13) | Phase D2, 2026-08-30. The index is declared `fts5(..., contentless_delete=1)`; the sync triggers are gone and every mutating path writes or deletes its FTS row in the chunk's own transaction, so `remove_by_file`, `replace_file`, and re-added chunk ids all retire their old terms. Pinned by `tests/unit/storage/test_contentless_fts.py` — the delete-stops-matching assertion a broken build cannot pass. The SQLite 3.43 floor is declared as `SqliteChunkRepository.MINIMUM_SQLITE_VERSION` and checked at open with an actionable message. `rebuild_fts()` re-tokenizes from `content` as the replacement for the retired `'rebuild'` statement, measured at 0.13 s over 3,022 chunks. |
| The storage simulator models a pre-C2 knowledge.db (BL-12) | Fixed 2026-08-30, before sizing D2. `storage_simulate.py` probes the artifact schema: relative paths strip `entities` and the `eid` codebook rather than running `REPLACE` over integer edge keys, `integer_edges` reports itself already applied instead of emptying the graph through its TEXT-key joins, and the contentless fold skips once `tokens_text` is gone. `chunking_projection.py` composes against both schemas. Re-measured against generation `20260830T043703346316Z-4b8e0856` (C1–C5 in the baseline): D2 is worth 4.64 MB, not §11's stale 5.64 MB; D3's `source_code` drop re-measures at 3.70 MB. The shared `CONTENTLESS_FTS` DDL now carries `contentless_delete=1`. |
| An in-memory vector plane is the wrong default at scale (BL-3) | **Rejected** 2026-08-30 under [DR-4](../research/storage_optimization_2026_v4.md). The item's substance was a disk-backed int8 ANN cache, measured in `storage_optimization_2026_v4.md` §5.2 at **recall@10 0.9950** against exhaustive fp32's 1.0000. That is a half-percent of top-10 recall traded for 18 MB, and no footprint number buys it. Its other half is already shipped: `VectorStore` builds `faiss.IndexIDMap2(faiss.IndexFlatIP(...))`, an exhaustive fp32 scan, so "skip the ANN index below the threshold" describes what runs today. What remains is a memory and open-time cost above roughly 100,000 vectors, which no repository this project indexes has reached. Re-file it as a footprint item if one does, with a width that keeps recall@10 at 1.0000. |
| Phase F cannot be built as specified, and would cost more than it saves (BL-14) | **Rejected** 2026-08-30 under [DR-4](../research/storage_optimization_2026_v4.md), and with it Phase F and its `chunk_source` escape hatch. Replacing `chunks.content` with byte-range descriptors leaves the exact plane with no implementation: `ExactQueryEngine` is `SELECT ... FROM chunks WHERE content LIKE ?` and nothing else. Measured over 300 mid-identifier fragments with LIKE as ground truth, the shipped `chunks_fts` plane answers them at **57.8% recall unbounded and 14.6% at the engine's limit of 10**, returning nothing at all for 109 of 300; a trigram index that would answer them exactly costs 10.00 MB against the 4.32 MB column it replaces. F also fails closed on an edited file, so the twelve paths touched in one working session would render **518 chunks, 7.6% of the index**, as empty hits while `chunks_fts` still matched their terms, and it takes away `rebuild_fts`'s only input, which is what makes the term index reproducible from the artifact alone. The saving is 3.50 MB, not §11's 6.39 MB. `zlib` level 6 on the same column saves 2.38 MB and `zstandard` level 10 with a trained dictionary saves 3.05 MB, both changing no plane, no semantics, and no freshness contract — take the bytes there. Reproduce either with `python scripts/exact_plane_recall.py --index knowcode_index` and `python scripts/storage_simulate.py --index knowcode_index --repo-root .`. The plan's "F stays gated on BL-14's prerequisites" now resolves to this row: F does not ship. |
| A fresh index under the default `entity_source` serves context with no source code (BL-16) | Fixed 2026-08-30, the day it was filed. `ContextSynthesizer` now chooses the read by *why* the text is missing rather than by whether the index is stale: a stale index still gets an unverified live slice, because the drift is what the caller asked to see; a fresh index with no stored copy (D3's `disk` default) gets `load_verified_source`, which serves the span only while it hashes to the digest taken at build time and nothing at all otherwise. `KnowCodeService.get_context` builds the loader unconditionally — gating its construction on staleness was the defect. Pinned by four rows added to `tests/unit/service/test_entity_source_resolution.py`, which now covers `get_context` alongside `get_entity_details`; the fresh-`disk` row failed before the fix. Both new guards were mutation-probed: swapping the verified read for an unverified one reddens the fail-closed row, and deleting the stale branch reddens the stale row. Full suite 1,857 passed, 3 xfailed. |
| A watched edit never advances the entity graph (BL-17) | Fixed 2026-08-30. A file transaction now rewrites the touched file's entity rows and the edges leaving them, from the same parse that produced its chunks: `PreparedFileUpdate` carries the parse's entities and relationships, `SqliteKnowledgeStore.replace_file` applies them in one writer transaction, and `StagedGenerationWriter` opens the staged `knowledge.db` and commits the graph half after the chunk half — that order makes a crash between the two land on the old behaviour rather than its worse inverse. The manifest's relationship count is read from the staged artifact instead of carried forward from the base. **Deliberately not fixed:** edges *arriving* from a file the transaction did not parse, which an incremental parse cannot re-derive; a rename still needs a full build to resolve everywhere. Five tests that pinned the old contract are inverted, not relaxed, including the e2e release-gate test that named the limitation — `knowcode doctor` stops reporting `store_stale_source_changed` after a watch commit, because the mtime that signal reads was the copied `knowledge.db`'s. Pinned by `tests/unit/service/test_watch_graph_updates.py`; all three production changes mutation-probed. The first probe of the edge deletion did *not* fire, because the assertion resolved edges through the `entities` table and so could not see an orphan — it now joins the `eid` codebook alone. Full suite 1,864 passed, 3 xfailed. |
| Dependency expansion relabelled a ranked hit as non-evidence (BL-18) | Fixed 2026-08-30, the day the business-logic audit filed it. `SearchEngine.search_scored` now emits every ranked hit as `retrieved`, in rank order, *before* expanding anything, and only then adds unseen callees as `dependency` with a zero score. The de-duplication guard can no longer reach a primary hit, so being some other hit's callee cannot cost a chunk its own retrieval evidence -- the loop order is what enforces it, which the docstring now says. Pinned by two tests in `tests/unit/retrieval/test_search_engine.py`: a three-link call chain with all three links ranked returned one `retrieved` label before the fix and three after, and a second test holds the other half, that a callee retrieval never ranked stays a `dependency` -- the guard against "fix" by relabelling everything `retrieved`. Both mutation-probed: moving the seeding loop back after expansion reddens the first, relabelling callees `retrieved` reddens only the second. The returned list changes from expansion-traversal order to rank order; `cli.py:438`, `mcp/server.py:343` and `api.py:195` all just iterate it, and `evidence[].rank` in the orchestrator now means retrieval rank, which is what it claimed. Full suite 1,866 passed, 3 xfailed. |
| The retrieval ladder was gated on the fail-closed flag, so every LLM answer got rung one (BL-19) | Fixed 2026-08-30. Both broadening rungs were guarded by membership in `local_answer_task_types`, which `AppConfig._fail_closed` empties on every load and no blessed policy artifact repopulates, so retrieval always stopped at rung one and the model was asked to answer from one entity's signature and docstring -- no source, no dependencies, under 1,500 tokens. The three copy-pasted rung blocks are now one `LADDER_RUNGS` tuple and one loop that stops at the first rung clearing the threshold. The ladder is climbed only when stopping early is *possible* -- some task type may answer locally and the caller has not demanded the LLM; otherwise one retrieval runs at the widest rung. That is the same single attempt as before, at full breadth rather than minimal, so the fix costs no extra retrieval in today's universal case. `force_llm` no longer thins the bundle either: it means "do not answer locally", not "retrieve less". Pinned by two tests in `tests/integration/test_agent_retrieval_contract.py`, which assert the whole kwargs dict one attempt asks retrieval for rather than one field at a time -- a bundle is thin because of the *combination*. Three mutation probes, each reddening only its own test: giving the un-routable case the thinnest rung, removing the early stop, and letting `force_llm` climb. The existing `retrieve_calls == 1` and `== 3` assertions still hold, so the token saving on a populated allowlist is unchanged. `docs/product/business-logic.md` restates the ladder. Full suite 1,868 passed, 3 xfailed. |
| Sufficiency saturated, and two task types cleared the routing gate with no source code (BL-20) | Fixed 2026-08-30. `_calculate_sufficiency` grew `max_score` inside the same branch that grew `score`, so the denominator described what the bundle happened to contain rather than what its task template asked for. Two consequences, both measured: every bundle holding everything its template named scored exactly 1.00 whatever the task type -- the number could not tell a rich `debug` bundle from a thin `locate` one -- and `extend` (0.91) and `locate` (1.00) cleared the 0.9 gate with no source code at all, `extend` while its own template asks for source. Both increments are now unconditional. With a fixed denominator the six task types land at 0.95-0.96 complete and 0.45-0.86 without source, so the gate discriminates in the direction it was built for. An entity with no docstring over 50 characters now tops out near 0.96 rather than 1.00, which is the honest reading. Pinned by `tests/unit/analysis/test_sufficiency_scoring.py`: source and docstring halves probed separately (each hoist reintroduced reddens one test and only one), an over-correction guard that a complete bundle still clears the gate, and an end-to-end EXTEND bundle through the real synthesizer that scored 0.91 before and blocks after. `docs/product/business-logic.md` now writes `max_score` out; leaving it undefined is what let the drift live. Full suite 1,881 passed, 3 xfailed. **Recalibrate the 0.9 floor** -- it was chosen against a formula that could only return 1.00 or block. |
| Telemetry reported a routing decision the router did not make (BL-22) | Fixed 2026-08-30. `retrieve_context_for_query` annotated `local_or_escalated` from `score >= sufficiency_threshold` (0.8), while the gate that actually routes is `max(sufficiency_threshold, routing_quality_floor)` (0.9) *and* membership in the always-empty `local_answer_task_types` -- and it did so from a path that reaches no routing decision at all, which is where MCP and REST callers land. Every retrieval scoring 0.8 or better was tallied into the local-answer rate that substantiates the token-savings claim, while the true rate was zero. The verdict now comes from `Agent._smart_answer`, from the *same expression* that picks the branch, so the metric and the answer cannot disagree; retrieval annotates the sufficiency measurement and nothing else. Absent now means "nothing was answered", not "escalated". Pinned on both sides of the move: `tests/unit/service/test_telemetry_events.py` asserts a bare retrieval scoring 0.9 emits no verdict and leaves `local_routing_rate` at 0.0, and four parametrized rows in `tests/integration/test_agent_retrieval_contract.py` assert the logged verdict equals what `smart_answer` returned -- compared against the actual result, never against a recomputed threshold, which would have drifted the same way the metric did. Three mutation probes; the sharp one puts the old 0.8 expression in the *new* location and still reddens the two empty-allowlist rows. `docs/user/telemetry.md` documents the presence rule and guards its `jq` example with `has(...)`. Full suite 1,887 passed, 3 xfailed. |
| Preflight accepted a weight set that turned an F into an A (BL-21) | Fixed 2026-08-30. `_parse_preflight_section` checked that each weight was a number and that its key was known, and nothing else -- neither sign nor sum -- while the composite normalised by `max(weight_sum, 1e-9)`. A set summing to zero therefore divided by `1e-9` and clamped to `overall_score = 1.0`, `overall_grade = A`, on a codebase whose `parse_success_rate` scored 0.0, clearing a `min_score` 0.9 build gate. One `validate_preflight_weights` rejects negative weights and any set that does not sum to 1.0 within `WEIGHT_SUM_TOLERANCE` (1e-6, generous beside the defaults' own 1.0000000000000002); the config parser calls it at load -- even when the section overrode nothing, so a drift in the defaults fails loudly -- and `assess_codebase` calls it too, being public and taking `weights` directly. The `1e-9` floor is gone: with validation it could only mask the bug it caused. Pinned by `tests/unit/analysis/test_preflight_weights.py`, both entry points, plus a guard that the two duplicate default tables in `config` and `preflight` have not drifted apart. Three mutation probes, each reddening only its own guard. **One existing test changed on purpose**: `test_custom_weights` passed partial overrides summing to 1.70, which is now rejected -- and it only asserted that both calls returned a float, so it could not have caught a composite that ignored the weights. It now puts the whole weight on one dimension and asserts the composite equals that dimension's score. Full suite 1,895 passed, 3 xfailed. |
| Live source reads were not bounded to the repository root (BL-25) | Fixed 2026-08-30. `LiveSourceLoader._slice` built its path as `self.root_dir / entity.location.file_path`, and `pathlib` discards the left operand entirely when the right is absolute -- which indexed paths *are*, since `normalize_file_identity` resolves every one. So the join bounded nothing, and a `..` segment walked out just as easily. Digest verification bounds what may be *served*, never what may be *read*, and `load_source` skips the digest altogether. A new `_resolve` sits under both reads and fails closed to `None` with a logged refusal, resolving root and candidate before comparing so a symlinked root -- macOS puts temporary directories behind one -- compares against the same real path the indexed identity was normalized to. Defense in depth: reaching it needs a foreign or tampered index. Worth closing because the loader now runs on the default read path for every `get_context` call, not only stale ones, since the BL-16 fix. Pinned by four tests in `tests/unit/analysis/test_live_hydration.py` -- absolute escape, `..` escape, a verified read whose digest genuinely *matches* the outside file (so only containment can refuse it, proved by the same entity being served when the loader is rooted at its own directory), and an over-rejection guard that an absolute path inside the root is still read. Mutation-probed. Full suite 1,899 passed, 3 xfailed. |
