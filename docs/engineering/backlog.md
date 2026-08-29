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

### BL-1 - Markdown documents are dropped from the index by an entity id collision

**Severity:** Critical. **Found:** 2026-08-29, while measuring the storage
baseline for Phase D1.

`MarkdownParser` gives the document entity the id `<file>::<file-stem>` and each
heading a section entity with the id `<file>::<heading-slug>`. When a document's
H1 slugifies to its own filename, which is the ordinary convention, the two ids
collide. The chunker then emits two chunks with the same id,
`validate_prepared_chunks` rejects the whole file, and `replace_file` keeps the
previous generation. On a first build there is no previous generation, so the
document is absent from the index entirely and the only signal is a `WARNING`
line.

Measured on this repository: **17 of 53 tracked markdown files, 259.9 KB, 32% of
prose bytes, are not indexed at all.** That includes seven of the eight ADRs,
`docs/engineering/architecture.md`, `docs/user/cli-reference.md`,
`docs/user/getting-started.md`, `docs/user/configuration.md`, and
`docs/user/rest-api.md`. An agent asking about any of them gets nothing.

Reproduce:

```bash
knowcode build . 2>&1 | grep "duplicate chunk id"
```

This is not the coverage defect in `storage_optimization_2026_v4.md` §6.2. That
one measured documents being truncated. These documents are absent. §6.2's
"39% of prose is unreachable" was computed assuming these files were indexed and
truncated, so the real figure is worse and the two must be fixed together.

**Owner:** none. Phase B of the storage plan is the natural home, because it
already rewrites markdown chunking. B3 would incidentally fix the chunk
collision, but the entity id collision in `knowledge.db` would remain, so B needs
an explicit item for it. Fix alongside BL-6 below, which is the same identity scheme failing a
different way.

**Not deferred on purpose.** It was found during Phase D1 and is out of that
phase's scope, not out of scope generally.

### BL-6 - Module chunks point at an entity that does not exist

**Severity:** High. **Found:** first in `docs/research/knowcode_evaluation_report.md`
as "Graph Resolution Mismatch"; re-confirmed 2026-08-29 and never tracked.

`Chunker._emit_module_chunks` tags the module-header and imports chunks with
`entity_id = f"{file_path}::module"`. No parser emits an entity with that id. A
markdown document is stored as `<file>::<doc-name>`, and a Python module under
its own identity, so the chunk carrying the largest single block of a file's
text is disconnected from the graph.

Measured on this repository over the chunker's output: **290 files and 594.7 KB,
14% of all chunk text**, sits in chunks whose `entity_id` matches no entity.
446.1 KB of that is markdown across all 53 files, 148.6 KB is Python across 237
files. For `docs/roadmap.md` the orphaned chunk is 16,788 bytes, most of the
document's indexed prose.

Do not add this to BL-1's 32%. These are chunker-output bytes; for the 17 files
BL-1 rejects outright, nothing is indexed at all and this defect is moot. The
overlap is why both belong to one fix.

The chunk text still reaches a semantic hit, so this is not total loss. What
breaks is every path that treats `chunk.entity_id` as a graph handle:

- `expand_dependencies` (`retrieval/completeness.py:41`) calls
  `get_callees("<file>::module")`, matches nothing, and expands nothing. Silent.
- `RetrievalOrchestrator` (`retrieval/orchestrator.py:199`) appends the dangling
  id to `selected_entity_ids` and counts it against `limit_entities`, so a
  nonexistent entity occupies a slot in the context budget and contributes no
  entity to the bundle.
- The evidence trail records an `entity_id` that resolves to nothing.

Reproduce:

```bash
python - <<'EOF'
from knowcode.indexing.chunker import Chunker
from knowcode.parsers import MarkdownParser
res = MarkdownParser().parse_file("docs/roadmap.md")
ids = {e.id for e in res.entities}
print(sorted({c.entity_id for c in Chunker().process_parse_result(res)} - ids))
EOF
```

Same area as BL-1 above. Both are markdown identity defects and both belong to the chunking phase of
[P7](../roadmap.md), but they are independent: BL-1 rejects whole files, this
one orphans chunks inside files that do index.

**Not deferred on purpose.** It was already written down once, in a research
report nothing links to, and lost. That is the reason this backlog exists.

### BL-2 - A full rebuild can silently drop a watch commit

**Severity:** High. **Found:** 2026-08-29, diagnosing a test failure during
Phase D1.

`KnowCodeService.build_generation` publishes without `expect_current`, so it
takes the pointer unconditionally. The watch writer's publication path does
compare-and-swap. The losing interleaving: a full rebuild scans the tree before
file `X` exists; the watch writer commits and publishes a generation containing
`X`; the rebuild finishes and moves the pointer to its own generation, built
from the earlier scan, which has no `X`. The watch batch published cleanly, so
`_operations` is empty, `has_pending` is false, and nothing re-derives it.

`tests/unit/service/test_watch_publication.py::test_concurrent_commits_and_publications_converge`
fails 13 runs in 40 on `main`. Phase D1 makes rebuilds faster, which widens the
window to 22 in 40. The race is independent of the storage work.

Reproduce:

```bash
for i in $(seq 1 40); do .venv/bin/python -m pytest "tests/unit/service/test_watch_publication.py::test_concurrent_commits_and_publications_converge" -q -p no:randomly 2>&1 | tail -1; done
```

The test asserts an invariant the code does not provide: "sustained contention
may refuse a publication, but never lose one." Either the code gains the
invariant or the test stops claiming it. Do not weaken the test to make it pass.

**Deferred on purpose.** Folding a publication-concurrency fix into a storage
change would have made both harder to review, and the defect predates that
change.

**In progress, uncommitted (2026-08-29).** A working tree change gives
`build_generation` an `expect_current` parameter and a bounded rebase loop
(`_REBUILD_REBASE_ATTEMPTS`), with a regression test named
`test_a_rebuild_never_reverts_a_concurrent_watch_publication`. Check whether
that landed before starting; do not duplicate it.

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

### BL-4 - `knowledge.db` carries unmeasured free-page slack

**Severity:** Medium. **Found:** 2026-08-29, comparing two storage baselines.

Between the audit build and the Phase D1 baseline, entities grew 11% and
relationships 10%, but `knowledge.db` grew 64%, from 19.48 MB to 31.93 MB. A
freshly built generation measured 30.51 MB against the baseline's 31.93 MB for
the identical corpus, which points at free pages rather than payload.

Phase A2 of the storage plan already proposes `VACUUM` before the manifest is
digested and estimates about 2 MB. That estimate predates this observation and
is probably low. Measure the free-page count with `dbstat` before sizing the
work.

**Owner:** Phase A2 of `storage_optimization_2026_v4.md`.

**In progress, uncommitted (2026-08-29).** A working tree change adds a
`compact()` path to `SqliteChunkRepository` with tests covering page reclamation,
byte-for-byte row preservation, a hot write-ahead log, idempotence, and use after
close. Check whether that landed before starting.

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
| Every embedding stored twice, 28.5% of a generation | Phase D1, 2026-08-29. [ADR 9](adr/adr-0009-derived-vector-plane.md). |
| Publication asserted the presence of a cache and compared two manifest numbers to check chunk/vector membership | Phase D1, 2026-08-29. The guard now counts durable embeddings in `chunks.db`, which is what [ADR 3](adr/adr-0003-durable-embedding-representation.md) already required. |
