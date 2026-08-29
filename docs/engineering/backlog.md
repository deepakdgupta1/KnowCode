# Engineering Backlog

Known defects and deferred work that no current workstream owns. This exists so
that something found while doing other work is not lost when that work ships.

The [roadmap](../roadmap.md) says what the project is building next. This says
what it already knows is wrong. An item leaves here when it is fixed, or when a
roadmap workstream adopts it and the row points at that workstream.

## Conventions

- One row per item, newest first within a severity.
- **Severity** is the user-visible consequence, not the effort. `Critical` means
  wrong or missing answers. `High` means a real defect with a workaround.
  `Medium` and `Low` are cost, clarity, and hygiene.
- Every item names how it was found and how to reproduce it. An item nobody can
  reproduce is a rumour.
- An item deferred on purpose says why, so the next reader does not re-litigate
  the decision.

## Open

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
index and touches all nine parsers. Phase C already rewrites id encoding, so
that is where it costs least. Do not paper over it with a reserved `::module`
token; ADR 1 rules out ad hoc internal namespaces, and BL-6 is the evidence
that inventing one produces ids nothing else agrees with.

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

### BL-3 - An in-memory vector plane is the wrong default at scale

**Severity:** Medium. **Found:** 2026-08-29, deliberate scope boundary of
Phase D1.

[ADR 9](adr/adr-0009-derived-vector-plane.md) makes the ANN index a plane
rebuilt in memory from the durable chunk rows. At 6,874 vectors that costs about
27 MB resident and 0.30 s per process open, which is free. Above roughly 100,000
vectors it is not.

Two pieces are specified in `storage_optimization_2026_v4.md` §5.3 and not
built. A disk-backed int8 ANN cache outside `generations/`, keyed by the
chunk-id digest the manifest already computes so retention never copies it,
measured at 0.995 recall@10 for a quarter of the fp32 size. And an exhaustive
fp32 scan below the threshold, where an index buys nothing at all.

**Deferred on purpose.** Neither is needed for the 32.31 MB Phase D1 recovered,
and building a cache tier before any repository needs one is speculative.

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
