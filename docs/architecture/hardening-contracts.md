# Parser, Index, and Security Hardening Contracts

**Status:** Accepted for the hardening blueprint

**Date:** 2026-08-12

**Scope:** Decisions required by Step 01 of the parser, concurrency, and
security hardening blueprint. These decisions freeze contracts for later
steps; they do not change parser, storage, indexing, API, or LLM behavior by
themselves.

## ADR 1: Entity and file identity

### Decision

A repository entity ID is:

```text
<canonical-file-identity>::<lexical-qualified-name>
```

`canonical-file-identity` is the absolute result of
`Path.resolve(strict=False)`, rendered with POSIX separators. Resolution is
performed at the scanner boundary and the value is passed unchanged to parser
entities, locations, chunks, manifests, watch events, replacement, and
removal. Resolving a path before it disappears makes `/var/...` and
`/private/var/...`, or a repository symlink and its target, one identity.
Symlinks are therefore identity aliases, not distinct source files.

Absolute IDs retain the current storage shape and avoid a cross-cutting root
parameter in every parser. They are intentionally machine-local. Moving a
repository or an artifact set to another root requires a rebuild; entity
content hashes remain available for rename correlation, not primary identity.

Qualified names include lexical scope. Examples include `outer.local`,
`Outer.method.local`, `Component.handler`, and `module.Type.method`. A direct
class-body callable is a `method`; a callable nested inside a function or
method remains a `function`. Python module assignments produce one variable
per simple bound name, including each simple name in chained or tuple
assignment. TypeScript interfaces, aliases, and enums remain `class` entities
until the shared entity-kind schema is deliberately expanded.

All code constructs use `build_internal_entity_id()`. Ad hoc internal-looking
categories such as `type::Point`, `trait::Display`, `::method::name`, and
`::data::name` are invalid.

### Reference namespaces

Relationship endpoints have exactly three classifications:

| Classification | Form | Meaning |
| --- | --- | --- |
| Internal | `<canonical-file>::<qualified-name>` | An entity extracted into this graph. |
| External | `external::<namespace>::<encoded-symbol>` | Known to be outside this indexed repository. |
| Unresolved | `unresolved::<language>::<encoded-file>::<encoded-scope>::<encoded-symbol>` | Not safely resolvable with available local symbols. |

Reference components are percent-encoded so quotes, Unicode, namespace
punctuation, and legal path characters remain data. `ref::name` and other
legacy pseudo-namespaces are invalid, not aliases for unresolved references.
Every parser edge source is an existing internal entity. Every internal target
is an existing entity, and both ends of `contains` are internal.

Ambiguous names remain unresolved. Graph resolution must never select the
first scan-order match. A known imported package may be external; a bare name
whose provenance is unknown is unresolved.

### Compatibility

Existing graphs containing unnormalized paths or legacy endpoint namespaces
must be rebuilt. Rewriting IDs in place cannot safely repair ambiguous scopes,
relationship endpoints, chunk IDs, or vector mappings.

## ADR 2: SQLite connection and transaction ownership

### Decision

Each worker or request execution context owns its SQLite connection. All
connections use WAL, `busy_timeout`, foreign-key enforcement, and the selected
durability setting. Unrelated threads never share a connection, including when
`check_same_thread=False` would permit it.

One repository-level writer coordinator serializes write transactions. A
public batch or replacement method owns its connection, lock, `BEGIN`, commit,
and rollback for the whole operation. Callers do not open an outer transaction
around individually locking repository methods. Reads use their context's
connection and observe committed snapshots only.

`close()` is idempotent. The bundle or service that creates a connection
factory owns it and closes connections only after active operation leases have
finished.

### Consequences

Step 08 adds transactional replacement; Step 09 changes connection ownership.
Tests must use barriers/events and the real shared-service topology. WAL alone
is not accepted as evidence of thread safety.

## ADR 3: Durable embedding representation

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

## ADR 4: Complete index generations

### Decision

The artifact root contains immutable generation directories plus one current
pointer:

```text
artifact-root/
  generations/
    <generation-id>/
      knowledge.db
      chunks.db
      vectors/...
      manifest.json
  current.json
```

A full or incremental build creates a unique same-filesystem staging
generation without holding the publication lock. It flushes and validates the
knowledge store, chunks, vectors, and manifest together. Validation covers
contract/schema versions, generation ID, checksums, embedding configuration,
counts, unique chunk IDs, and chunk/vector membership.

After validation, publication takes the generation write lock, finalizes the
staged directory, and atomically replaces `current.json` last. Readers acquire
one generation lease and use it for sparse, dense, graph, and context work.
Failure before pointer replacement leaves the old generation searchable.
Superseded generations are retired only after reader leases end, and at least
one last-known-good generation is retained.

Replace-style JSON metadata uses a same-directory temporary file, flush, file
`fsync`, `os.replace`, and parent-directory `fsync` where supported. Data is
published before its manifest, and the generation pointer is published last.

### Recovery and rollback

Startup validates the pointed generation. An invalid pointer or generation may
fall back only to a completely valid retained generation; individual artifact
files are never mixed. Orphan staging directories are ignored and later
cleaned. Rollback moves the pointer to a compatible complete generation or
rebuilds; it never downgrades one artifact in place.

## ADR 5: Telemetry privacy

### Decision

Default telemetry is an allowlisted aggregate event schema. A query event may
contain classification, length bucket, routing result, sufficiency bucket,
duration, outcome, and a privacy-reviewed keyed correlation value. It does not
contain raw query text, retrieved code, prompt bodies, tokens, credentials, or
arbitrary caller-supplied nested fields.

The sink also applies recursive secret redaction and field/record length
bounds as defense in depth. Logs are local, created with mode `0600`, rotated,
retained for a bounded period, and deletable through a documented local
operation. One logical query produces one counted query event.

Raw query capture, if retained at all, is a separate explicit opt-in with a
warning, separate file, short retention, and tests. Redaction alone is not a
safe default.

## ADR 6: Direct-server proxy trust

### Decision

The normal local Uvicorn server explicitly disables proxy-header processing.
KnowCode does not currently support proxied deployment, so it exposes no
trusted-proxy option in this hardening series. `X-Forwarded-For` and
`Forwarded` are ordinary untrusted headers in direct mode.

Future proxy support requires a separate design with an explicit narrow IP or
CIDR allowlist. Wildcard trust is rejected by default. Rate-limit tests must
start the real server stack with the limiter enabled; changing SlowAPI's key
function without changing Uvicorn trust is not a fix.

## ADR 7: Protocol and artifact evolution inventory

Steps 08 through 13 make the following contract changes. Step 01 records them
but does not change runtime protocols.

| Surface | Current baseline | Required contract | Owner |
| --- | --- | --- | --- |
| `CodeChunk` | Embedding is transient and optional. | Validated durable float32 embedding plus dimension for dense generations. | Step 08 |
| `ChunkRepository` | `add`, `add_batch`, then destructive `remove_by_file`. | Transactional `replace_file(file_id, chunks)` returning previous IDs, committed IDs, and generation metadata; durable embedding iteration and schema inspection. | Step 08 |
| SQLite connections | One `check_same_thread=False` connection per repository instance. | One connection per execution context and one serialized transaction owner. | Step 09 |
| `VectorStoreProtocol` | `add`, logical `remove`, search, count, save/load; no generation or locking contract. | Exact-ID idempotent `upsert`, exact `remove`, unique search results, live count, dimension validation, flush/close, contract version, generation ID, and serialized/snapshot-safe operations. | Step 10 |
| FAISS metadata | Schema 2; ID map can diverge from native rows. | Schema 3 with backend, dimension, contract version, generation, checksums, native/live count parity, and exact upsert/removal. | Step 11 |
| LanceDB metadata | Schema 1; interpolated filters and unsynchronized buffer. | Schema 2 with the shared vector contract, digest-keyed exact matching, generation metadata, and synchronized buffer/table state. | Step 12 |
| JSON/manifest writes | Several files truncate in place. | Shared crash-safe atomic replacement utility and pointer-last ordering. | Step 13 |
| Index manifest | Schema 2; no complete-generation parity. | Schema 3 with artifact contract version, generation, checksums, embedding config, and knowledge/chunk/vector counts. | Steps 13-14 |

Required vector operations are statically typed. `hasattr` is not capability
negotiation. An incompatible implementation raises an explicit contract or
artifact-version error.

### Existing-artifact policy

| Artifact | Baseline version | Outcome under the generation contract |
| --- | --- | --- |
| JSON knowledge store | 2 | May be read by the legacy exporter, but cannot be mixed into a current generation; rebuild for publication. |
| SQLite knowledge store | 1 | Rebuild into a staged generation because canonical IDs and generation metadata cannot be inferred safely. |
| SQLite chunk store | 1 | Fail closed and rebuild; embeddings are absent. |
| Index manifest | 2 | Fail closed for generation loading; checksums and parity are absent. |
| FAISS metadata | 2 | Rebuild from durable chunks into schema 3; never reuse a possibly divergent ID map. |
| LanceDB metadata | 1 | Rebuild from durable chunks into schema 2; do not rewrite potentially injected/duplicate rows in place. |

An artifact with a missing, malformed, newer, or unsupported version produces
one actionable message naming the incompatible artifact and rebuild command.
No loader silently stamps the current version onto an unverified legacy
payload.

## Vector contract (Step 10)

Step 10 freezes the `VectorStoreProtocol` mutation and persistence contract
shared by the FAISS/NumPy and LanceDB backends. It adds no production callers
and changes neither backend's on-disk format; it makes the existing semantics
explicit and testable so Steps 11 (FAISS) and 12 (LanceDB) repair backends
against one shared definition.

### Protocol surface

`VectorStoreProtocol` is `@runtime_checkable`. Conformance is checked with
`isinstance` (the `dimension: int` data member makes `issubclass` raise
`TypeError`, as expected for a non-method protocol member). The required members
are `add`, `upsert`, `flush`, `search`, `get_embedding`, `save`, `load`,
`clear`, `remove`, `count`, and the `dimension` attribute. `hasattr` is not
capability negotiation: a missing required member is a contract violation.

`upsert` and `flush` are the only members new to Step 10; `remove` and `count`
already existed. `upsert(id, embedding)` is the exact-ID idempotent
add-or-replace. `flush()` is the write-visibility boundary: it is a no-op on
FAISS/NumPy (which commits each `add` immediately and has no buffer) and drains
LanceDB's mutable write buffer into the durable table.

### Typed errors

`knowcode.errors` gains three typed exceptions, all deriving from
`VectorContractError(RuntimeError)`:

- `VectorDimensionError` — raised by `add`/`upsert` when `len(embedding) !=
  dimension`. Verified against the baseline: a mismatch already failed today,
  but unhelpfully — FAISS raised a bare `AssertionError` with an empty message,
  the numpy fallback raised `ValueError` from `np.vstack`, and LanceDB raised a
  low-level Arrow `ValueError` at *flush* time on an unrelated later call. The
  contract requires validation at `add`/`upsert` time with a named expected and
  actual dimension.
- `VectorArtifactVersionError` — the fail-closed signal a loader raises for a
  missing, malformed, newer, or unsupported persisted artifact. Step 10 freezes
  the type and rebuild guidance; Steps 11/12 stamp and validate the version.
- `VectorContractError` — the base category for the above.

### Locking and snapshot matrix

| Operation | Semantics | Lock ownership |
| --- | --- | --- |
| `add`, `upsert`, `remove`, `clear` | serialized mutation | mutation lock |
| `flush` | serialized with mutation | mutation lock |
| `search`, `get_embedding`, `count` | snapshot-safe read | read observes one generation |
| `save`, `load` | serialized with mutation and each other | mutation lock |

A read observes one consistent generation: a buffered or in-flight write is
either fully visible or not visible at all, and `flush()` is the boundary that
makes buffered writes visible. Today only LanceDB has a write buffer; its
`search`, `get_embedding`, `save`, `remove`, and `count` already flush
internally, and Step 10 exposes that as the required `flush()` member so "when
is a write visible" is defined rather than accidental. The per-generation
reader-lease handoff, `close()`, and generation IDs are declared as intent in
the protocol docstrings and are implemented in Steps 11/12/18; Step 10 does not
touch the artifact format.

### Deferred defects (strict xfails), now all closed

The shared contract suite (`tests/helpers/vector_assertions.py` +
`tests/unit/storage/test_vector_contract.py`, parametrized over FAISS, the numpy
fallback, and LanceDB) is green overall. The following desired-contract cases
failed at Step 10 and carried named strict `xfail` marks, so each became an
XPASS failure the moment its step repaired the behavior:

| Case | Backends | Repair step | State |
| --- | --- | --- | --- |
| Native-tombstone parity (`index.ntotal == count()`) | VectorStore (FAISS, numpy) | 11 | closed |
| Removed ID consumes a top-k slot | VectorStore (FAISS, numpy) | 11 | closed |
| Duplicate `add` of one ID (`count() == 2`) | VectorStore (FAISS, numpy) | 11 | closed |
| Duplicate `add` of one ID (`count() == 2`) | LanceDB | 12 | closed |
| Result-ID uniqueness after a duplicate `add` | VectorStore (FAISS, numpy) | 11 | closed |
| Result-ID uniqueness after a duplicate `add` | LanceDB | 12 | closed |
| Hostile-ID removal widening (`x' OR true --`) | LanceDB | 12 | closed |
| Hostile-ID `get_embedding` widening | LanceDB | 12 | closed |
| Hostile-ID `upsert` widening (inherits `remove`) | LanceDB | 12 | closed |

Every row is now a live gate in `test_vector_contract.py` on all three backends,
not an xfail: each XPASS-failed the moment its step landed, which is what the
strict marks were for. No strict xfail remains in the vector suites.

VectorStore matched IDs exactly all along, so its hostile-ID cases were green
controls for the LanceDB repair.

### Incompatible-artifact policy

Step 10 changed no on-disk artifact. Steps 11 and 12 then stamped and validated
both envelopes: FAISS metadata is schema 3 and LanceDB metadata is schema 2, and
an artifact that is missing, malformed, legacy, newer, or inconsistent with its
index is rejected with `VectorArtifactVersionError` and `knowcode build` rebuild
guidance rather than being migrated in memory.

## LanceDB backend (Step 12)

Step 12 repairs the reviewed injection defect, activates the Step 10 contract
for LanceDB, and synchronizes its mutable write buffer with the durable table.

### Chunk IDs are data, not filter syntax

`get_embedding` and `remove` interpolated the chunk ID straight into LanceDB's
SQL-like predicates (`where(f"id = '{chunk_id}'")`, `delete(f"id =
'{chunk_id}'")`). The reproduced repository ID `x' OR true --` therefore widened
a read to a different row and deleted every row in the table.

The locked LanceDB version (0.33; floor 0.12) accepts only string predicates —
`Table.delete(where: str)`, `count_rows(filter: str)`, and `.where(str)` — so
exactness cannot come from a typed expression API. It comes from the column the
predicate names instead:

* Every row carries `key`, the SHA-256 hex digest of its chunk ID, alongside
  `id` and `vector`.
* Every predicate this store issues is built by one central pair of helpers,
  `_key_predicate` (single) and `_keys_predicate` (batched flush), from that
  digest. `_key_literal` asserts `^[0-9a-f]{64}$` before quoting, so a
  programming error cannot smuggle anything else in either.
* A digest cannot contain a quote, operator, comment, or whitespace, so no
  repository ID can reach the filter grammar at all. This is a stronger
  guarantee than quote escaping, which depends on the SQL dialect's lexer, and
  it is **not** a filename whitelist: every legal path, including quotes,
  backslashes, Unicode, and SQL wildcards, is stored and matched unchanged.
* Exactness is then verified in Python against the returned row's own `id`, so
  matching is proven on the data rather than assumed from the predicate.
* A digest collision between two live chunk IDs is the one remaining way
  exactness could be lost. It is rejected with `VectorContractError` on write
  and `VectorArtifactVersionError` on load, never silently widened.

### Buffer and table synchronization

One re-entrant lock serializes `add`, `upsert`, `remove`, `clear`, `flush`,
`save`, `load`, `search`, `get_embedding`, and `count`, satisfying the Step 10
matrix. Visibility is defined rather than accidental: **reads flush first**, so a
write that has returned is always observable by a later read on that store.

The buffer is keyed by chunk ID and paired with a pending-delete set, so:

| Sequence | Effect |
| --- | --- |
| `add` of a buffered ID | replaces the buffered row in place; nothing reaches the table |
| `add` of a durable ID | buffers the new row and schedules the stale durable row for deletion |
| `remove` of a buffered ID | drops the buffered row; nothing reaches the table |
| `remove` of a durable ID | schedules the durable row for deletion |
| `flush` | applies pending deletes **first**, then inserts, so a replacement never leaves two rows |
| `load` | discards any pending buffer; a rejected artifact leaves live state untouched |

Deletes are issued in batches of 128 digests so one `IN (...)` predicate stays
bounded. `add` is exact-ID add-or-replace and shares one code path with
`upsert`, so one chunk ID always has exactly one live row and search results are
unique. `count()` and `ids()` report the live set, and the tests assert them
against the durable rows read straight out of the table — bookkeeping alone
cannot hide a widened delete.

### Metadata envelope, schema 2

`vectors.json` records `schema_version`, `backend`, `dimension`, live `count`,
and `generation`. `load()` validates the whole set before adopting any of it:

| Check | Rejected when |
| --- | --- |
| Envelope version | missing, malformed, legacy (1), or newer than 2 |
| Backend | the envelope names another backend |
| Dimension | not an integer, or disagreeing with the table's vector width |
| Artifact | the envelope exists without its `vectors.lancedb` table, or the reverse |
| Table schema | the `key` column is absent (a pre-Step-12 table) |
| Duplicate IDs | two rows carry one chunk ID |
| Key collision | two chunk IDs share one digest |
| Declared count | `count` disagrees with the rows the table holds |

A schema 1 envelope is not migrated in memory. It describes a table with no
exact-key column whose rows may also carry duplicates or the residue of an
injected delete, so `knowcode doctor` reports it as a failure with rebuild
guidance. The committed `knowcode_index/vectors.json` is exactly such a v1
artifact and must be rebuilt with `knowcode build`.

A path with no persisted table loads as an empty index. The directory alone is
not evidence: `lancedb.connect` creates it, so a store merely pointed at an
index directory has one with no table inside — which is the normal service
topology on a first build.

`save()` is symmetric on every configuration. A directory-backed store already
living at the destination writes its envelope in place; anything else, including
an in-memory store, is exported into a fresh database at the target so rows are
never silently dropped.

## FAISS/NumPy backend (Step 11)

Step 11 repairs the reviewed removal defect in `VectorStore`, the one class that
covers both the FAISS engine and its numpy fallback.

### ID-aware native index

`remove()` used to delete only the `id_map` entry, so the vector stayed in the
native index. Both engines are now ID-aware and removal deletes the row itself:

* FAISS wraps `IndexFlatIP` in `IndexIDMap2`, giving `add_with_ids`,
  `remove_ids`, and `reconstruct` by assigned ID.
* `MockVectorStore` mirrors that surface with numpy, deriving `ntotal` from the
  row count so it cannot drift, and returning native IDs (not row positions)
  from `search`.

Tombstones are removed, never hidden by overfetching or by filtering a larger
top-k. `count()` equals the native row count, and a removed ID frees its top-k
slot for the next live result.

### Exact-ID assignment

Each chunk ID maps to one monotonically assigned native ID (`id_map` is native
ID → chunk ID, with a private reverse map). `add()` is exact-ID *add-or-replace*
and shares one code path with `upsert()`: re-indexing a chunk replaces its
single live row rather than adding a second row that would occupy a result slot
and double the chunk's weight in fusion scoring. ID assignment resumes above
every persisted native ID after a load, so a reused ID cannot collide.

### Locking

One re-entrant lock serializes `add`, `upsert`, `remove`, `clear`, `search`,
`get_embedding`, `count`, `save`, and `load`, which satisfies the Step 10
matrix: mutations are serialized and reads observe one generation. `flush()`
remains a no-op — every mutation commits immediately, and there is no buffer.

### Metadata envelope, schema 3

`vectors.json` records `schema_version`, `backend` (`faiss`, the registry key
shared by both engines), `engine` (`faiss` or `numpy`, the artifact's actual
writer), `dimension`, live `count`, `generation`, `next_native_id`, and
`id_map`. The numpy engine additionally records `row_ids`, the native ID of each
saved `.npy` row in order, because — unlike a FAISS artifact — the `.npy` file
carries no IDs of its own.

`load()` validates the whole set before adopting any of it:

| Check | Rejected when |
| --- | --- |
| Envelope version | missing, malformed, legacy (1/2), or newer than 3 |
| Engine | the artifact was written by the other engine |
| Native artifact | `vectors.json` exists without its `.index`/`.npy`, or the reverse |
| Dimension | the declared dimension disagrees with the loaded index |
| ID-map/native parity | the mapped IDs are not exactly the index's native IDs |
| Declared count | `count` disagrees with the ID map it summarizes |
| Duplicate chunk IDs | two native IDs map to one chunk ID |

Every rejection raises `VectorArtifactVersionError` naming the artifact and the
`knowcode build` rebuild command. A path with no vector artifacts at all still
loads as an empty index, which is how a fresh `Indexer` reads a directory that
was never saved.

Schema 1 and 2 envelopes are no longer migrated in memory. A v1/v2 artifact
describes a plain `IndexFlatIP` whose rows carry no native IDs, so its ID map
cannot be verified against the index; adopting it is exactly the drift this step
removes. `knowcode doctor` reports such an artifact as a failure with rebuild
guidance rather than as a migration warning.

Artifact checksums, which ADR 7 lists in the same row, stay with Steps 13 and 14:
they belong to the crash-safe writer and the cross-artifact generation
validation, not to one backend's envelope.

## Crash-safe artifact replacement (Step 13)

Before Step 13, every replace-style JSON artifact truncated its target in place
(`open(path, "w")` followed by `json.dump`). The failure was reproducible
without any fault injection: saving a `KnowledgeStore` whose entity metadata
contained an unserializable value raised `TypeError` *after* the file had been
emptied, and the previous graph then failed to load at all
(`json.decoder.JSONDecodeError: Expecting value`).

`knowcode.utils.atomic_write` is the one writer for these artifacts.

### The replacement sequence

1. Serialize the payload **before** touching the filesystem.
2. Create a staging file in the **same directory** as the target, named
   `.<target>.pid<pid>.<random>.knowcode-tmp`, so the later rename cannot cross
   a filesystem boundary.
3. Write, `flush`, and `fsync` that file.
4. `chmod` to an explicit mode (`0644` by default, not the umask or `mkstemp`'s
   `0600`) and `os.replace` it onto the target.
5. `fsync` the parent directory where the platform supports it.

`atomic_write_json` covers JSON payloads; the `atomic_replacement` context
manager covers artifacts written by a third-party writer that owns the format
(`faiss.write_index` and the numpy fallback's `.npy`).

| Injected failure | Outcome |
| --- | --- |
| Serialization | `TypeError`; no staging file is ever created |
| Short write (`ENOSPC`) | `OSError`; staging file removed |
| File `fsync` | `OSError`; staging file removed |
| `os.replace` | `OSError`; staging file removed |
| Parent-directory `fsync` | logged, not raised: the replacement already succeeded |
| Writer produced no staged file | `FileNotFoundError`; the target is not deleted |

In every raising case the previous artifact is byte-identical and still loads.
A directory `fsync` failure is deliberately non-fatal — by then `os.replace`
has returned, so the caller's "the old file or the new file, never a partial
one" invariant already holds and raising would misreport a successful write.

One behavior change comes with the rename: `open(path, "w")` followed a
symlinked target, whereas `os.replace` replaces the symlink itself with a
regular file. That is inherent to atomic replacement — writing through the link
would mean truncating the destination in place — and no KnowCode artifact is
published through a symlink.

### Publication ordering

Data is published before the metadata naming it:

| Writer | Order |
| --- | --- |
| `VectorStore.save` | `.index`/`.npy`, then `vectors.json` |
| `LanceDBVectorStore.save` | table, then `vectors.json` |
| `Indexer.save` | vectors, chunks, then `index_manifest.json` |

A crash therefore leaves at worst an older manifest beside present data, never
a manifest naming data that was never written. The generation pointer that
makes this ordering a full commit protocol is Step 14; Step 13 supplies the
primitive and the ordering it publishes with.

### Startup hygiene

`Indexer.load` and `KnowledgeStore.load` call `cleanup_orphaned_temp_files`,
which removes staging files whose embedded PID is not the current process. Its
own process's staging files are skipped, since another thread may be mid-write.
ADR 4 gives one writer process per artifact root, so a staging file from any
other PID is by definition abandoned.

Truncated metadata now fails closed rather than surfacing a raw
`json.JSONDecodeError`: both vector backends raise `VectorArtifactVersionError`
and the knowledge store and index manifest raise `ValueError`, each naming the
artifact and the `knowcode build` rebuild command.

### Out of scope here

Artifact checksums, cross-artifact generation validation, and the `current.json`
pointer are Step 14. `LanceDBVectorStore._export_locked` still deletes its
target directory before copying a replacement; that path runs only for a store
that does not already live at the target (never on the production save path),
and directory-level staging belongs to Step 14's generation directories.
Append-only telemetry does not use this module: it has no previous version to
preserve.

## Complete index generations (Step 14)

Step 14 implements ADR 4. Before it, a full rebuild mutated the live artifacts
in place and in the wrong order: it deleted the semantic index directory,
committed `knowledge.db`, then *tried* to rebuild the index and reported a
failure as a successful analysis. One failed rebuild published a new graph with
no index and destroyed the last good semantic generation.

### Layout

```text
knowcode_index/
  current.json                       # the pointer readers resolve, published last
  generations/
    <generation id>/
      knowledge.db                   # the graph
      chunks.db                      # the chunk store
      vectors.json + vectors.index | vectors.npy | vectors.lancedb/
      index_manifest.json            # embedding/chunking config
      manifest.json                  # the generation manifest, schema 3
  .staging-<generation id>.pid<pid>/  # a generation under construction
```

A generation id is `YYYYMMDDThhmmssffffffZ-<8 hex>`. Lexical order is
publication order, because retention and pointer fallback both mean "the newest
valid generation"; the microsecond stamp orders builds across processes and an
in-process guard keeps two builds in the same microsecond strictly ordered.

### Build sequence

1. Parse the repository once. The resulting `GraphBuilder` carries its scanned
   files and parse results, and the *same* builder writes `knowledge.db` and
   feeds the chunker — so a generation can never hold chunks for files its
   graph never saw. A parse failure propagates; nothing has been staged.
2. Stage `knowledge.db`, then `chunks.db`, the vector artifacts, and
   `index_manifest.json` into `.staging-<id>.pid<pid>/`. The live generation is
   neither read nor mutated. An incremental build copies the previous
   generation into staging first; Step 15 replaces that copy with a validated
   copy-on-write delta.
3. Close every store, so the committed rows are in the database files rather
   than an open write-ahead log.
4. Write `manifest.json` (data before the metadata naming it), then validate
   the staged set *together*: manifest schema, generation id, artifact
   presence, chunk/vector count parity, the entity- and chunk-id digests, and
   every artifact checksum.
5. Take the publication lock, `os.replace` the staging directory into
   `generations/<id>`, `fsync` the parent, then atomically replace
   `current.json` **last**.
6. Retire superseded generations, never the current one.

A failure at any step before the pointer write leaves the previous generation
current. A failure of the pointer write itself also removes the just-renamed
directory, so a later fallback cannot select a generation that was never
published.

### Generation manifest, schema 3

| Field | Purpose |
| --- | --- |
| `schema_version`, `generation_id`, `created_at`, `kind` | Identity; fails closed on any other schema. |
| `counts` | `entities`, `relationships`, `chunks`, `vectors`. |
| `digests` | `entity_ids` and `chunk_ids`: SHA-256 over the sorted id set. |
| `embedding`, `vector` | Embedding config and vector backend/dimension. |
| `schema_versions` | Chunk store and index-manifest versions. |
| `artifacts` | Name, kind, SHA-256, and size of every immutable artifact. |

SQLite databases are validated *logically* (id digests plus counts) rather than
by file checksum: opening a WAL database rewrites its bytes at checkpoint time,
so a byte checksum over `chunks.db` would false-alarm on the first reader.
Immutable artifacts — the vector metadata envelope, the native vector index,
the indexer manifest — are checksummed by content. Validation reads databases
through an `immutable=1` URI when no write-ahead log is present, so validating
a generation does not write to it.

Vector-side id parity is asserted in memory at build time, where both id sets
are known, and recorded as `counts.vectors`; the vector protocol exposes no id
enumeration, so on load the check is count parity against the chunk digest.

### Validation depth

| Caller | Checks |
| --- | --- |
| Publication | Structural **and** digests/checksums. |
| Startup, `ensure_index()`, reload | Structural only: schema, id, presence, count parity. Cheap enough to run on every service start. |
| `knowcode doctor` | Structural **and** digests/checksums, as the "Index generation" check. |

### Recovery, retention, and disk

`resolve_current_generation` prefers the pointed generation and falls back to
the newest *completely valid* retained generation; it never mixes artifacts and
never falls back to individual files. With no usable generation it returns
`None`, and the caller raises the ordinary missing-store/missing-index error.

`DEFAULT_RETAINED_GENERATIONS` is 2: one live generation plus one
last-known-good fallback. Each generation is a full copy of the index, so this
is also the disk-footprint multiplier — a 400 MB index occupies roughly 800 MB
across a rebuild, and an incremental build briefly needs a third copy while its
staging directory exists. The current generation is never retired regardless of
the retention setting.

Staging directories carry the owning PID. A build removes staging directories
belonging to *other* processes before starting (ADR 4 gives one writer process
per artifact root) and never its own, since another thread may be mid-build.
Readers ignore staging directories entirely.

### Graph-only generations

A generation is `full` (graph plus semantic index) or `graph_only`. When the
semantic phase fails:

* if the current generation *has* a semantic index, nothing is published — the
  build reports `published=False` with a classified stage and the searchable
  generation survives;
* if there is no semantic generation to protect, the graph is published as an
  explicit `graph_only` generation, so a build without usable embeddings yields
  a usable graph instead of nothing.

A `graph_only` generation is not searchable: `_assert_index_exists` raises
`MissingSemanticIndexError` rather than presenting an empty index as working,
`ensure_index()` rebuilds, and `doctor` warns. Its directory holds no chunk or
vector artifacts, so opening a reader can never create `chunks.db` inside a
published generation.

### Failure classification

`analyze()` no longer reports a failed index build as success. It returns
`published`, `generation_id`, `generation_kind`, and — on failure —
`index_error` with `index_error_stage` (`knowledge_store`, `semantic_index`, or
`publication`). `knowcode build`, `knowcode analyze`, and `knowcode index` exit
non-zero when nothing was published and say that the previous generation is
still current.

### Compatibility

An install with no `current.json` keeps the pre-Step-14 flat layout: the
knowledge store resolves to `knowledge.db`/`knowcode_knowledge.json` beside the
sources and the chunk/vector artifacts to `knowcode_index/` directly. Nothing
migrates the flat layout in place — the legacy chunk and vector artifacts
already fail closed under Steps 08, 11, and 12 — so the next `knowcode build`
publishes a generation and `doctor` warns about the flat layout until then. A
stale flat `knowledge.db` left behind is ignored once a generation exists.

## Incremental file update transactions (Step 15)

Step 14 made a *full* rebuild all-or-nothing. Step 15 does the same for a single
file, which is what watched edits, `index_file`, and incremental builds actually
commit. Before it, every one of those paths removed a file's chunks *before* its
replacement existed:

```text
remove_file(path)     # the previous generation is gone
index_file(path)      # ... and this is allowed to fail
```

Reproduced with only a failing provider: a file indexed as `chunks=2 vectors=2`
became `chunks=0 vectors=2` after one failed re-index, and dense search kept
answering with both deleted chunk ids.

### Prepare, then commit

| Phase | What it does | On failure |
| --- | --- | --- |
| `Indexer.prepare_file_update` | Parse, chunk, reuse durable embeddings for unchanged content, embed the rest, validate count/dimension/finiteness/id-uniqueness. Reads live state, writes none. | `FileUpdatePreparationError`; the previous generation is untouched. |
| `Indexer.commit_file_update` | `ChunkRepository.replace_file` in one writer transaction, then vector `upsert` for every committed id and `remove` for every id that did not survive, then `flush`. | Vector failure is rebuilt from the durable chunk rows; only an unrecoverable one raises `FileUpdateCommitError`. |

A prepared update is a complete replacement, never a delta: committing it makes
exactly its chunks the file's searchable set, whatever was there before. One
commit lock serializes the pair, so two files' transactions cannot interleave.

`index_file`, `remove_file`/`delete_file`, `move_file`, `index_directory`, and
`index_incremental` are all thin wrappers over that primitive, and the watch
worker calls `replace_file`/`delete_file`/`move_file` directly. No `hasattr`
capability probe remains on any of them.

### Recovery source

After the SQLite transaction commits, the chunk rows — which carry durable
float32 embeddings since Step 08 — are the committed truth. A vector failure is
therefore repaired by re-deriving the affected rows from the chunk store, not by
re-embedding and not by trusting the previous vector state. If that still fails,
the commit raises and the caller discards its unpublished generation.

### Deletion versus failure

| Situation | Outcome |
| --- | --- |
| File is gone, or its extension is no longer indexable | Deletion: chunks and vectors are removed together. |
| File parses cleanly and yields no chunks (an empty file) | Deletion. |
| File exists but its parse reported errors and yielded no chunks | **Preparation error.** A file saved mid-edit with a syntax error keeps its previous chunks instead of dropping out of the index. |
| Partial extraction (some entities, some errors) | Committed as prepared; the errors ride along on `PreparedFileUpdate.parse_errors`. |

A bulk pipeline (`index_directory`, `index_incremental`) does not abort on one
such file: it keeps that file's previous generation and records the reason in
`Indexer.failed_updates`, which is logged rather than swallowed.

### Move ordering

The destination is prepared and committed *before* the source is dropped. An
embedding failure therefore leaves the source searchable, and the worst
post-commit outcome is a duplicate that a retry clears — never a file that
exists under neither identity. A move whose source and destination normalize to
one identity commits once and removes nothing.

### Publication

Incremental builds publish through Step 14's pointer: `build_generation(
incremental=True)` seeds a staging directory from the last published
generation, applies the per-file transactions inside it, validates the staged
set, and replaces `current.json` last. The live generation is never edited in
place, so its manifest digests stay true and a reader on it sees a stable index.
Staging remains a full copy rather than a copy-on-write delta — ADR 4 permits
either, and a hardlinked delta is unsafe for SQLite, which writes pages in
place. The disk cost is the one documented with retention.

### One read snapshot per query

`HybridIndex.search` binds the chunk repository and vector store once for the
whole query, so a reload between the sparse and dense reads cannot answer half a
query from each generation. Ids from either retriever are de-duplicated before
fusion — a repeated id would otherwise score twice — and a dense id the
repository cannot resolve is skipped without consuming one of the `limit` result
slots.

## Watch queue semantics (Step 16)

Step 15 made one file's update a transaction. Step 16 makes the *stream* of
filesystem events that drives those transactions lossless: events converge on
the index the final filesystem state implies, and anything that does not make it
in is visible rather than silent.

Reproduced against the pre-Step-16 worker through its production API only:

| Scenario | Before | After |
| --- | --- | --- |
| Five modify events for one file | 5 commits | 1 commit |
| `m.py` and `sub/../m.py` | 2 work items | 1 |
| `start()` called twice | 2 threads over 1 queue, 2 commits running at once, 1 joined | 1 worker; the second call is a no-op returning `False` |
| `stop()` with 3 items queued | 1 committed, 2 dropped, returned `None` | 3 committed, `DrainReport(completed=True)` |
| Provider outage during a re-index | update dropped; nothing retried, nothing reported | retried with backoff; committed, or reported in `failures()` |
| Event under `node_modules/` | indexed, though no build would include it | ignored, by the scanner's own rules |

### Coalescing rules

`WatchQueue` holds at most one item per canonical file identity (ADR 1). Events
do not accumulate — they collapse into the single replacement the file's final
on-disk state implies, and committing that one item produces the same index as
replaying every event.

| Pending | Incoming | Result |
| --- | --- | --- |
| `index(p)` | `index(p)` | `index(p)` — one commit |
| `index(p)` | `delete(p)` | `delete(p)` |
| `delete(p)` | `index(p)` | `index(p)` |
| `index(b)` dropping `a` | `index(b)` | `index(b)` dropping `a` — a modify after a rename must not leave the old identity indexed |
| `index(b)` dropping `a` | `move(b → c)` | `index(c)` dropping `a` and `b` — the whole chain, one commit |
| `index(b)` dropping `a` | `index(a)` | `index(b)` dropping nothing, then `index(a)` — `a` exists again, so no earlier move may drop it |

Two rules keep coalescing from changing what gets committed:

* **Submission order is preserved.** Coalescing updates an item in place rather
  than moving it to the tail, so a file saved in a loop cannot starve the files
  queued behind it.
* **In-flight work never absorbs an event.** A commit that is already running
  read the file before the new event happened, so a new event schedules a fresh
  item instead of being folded into a stale read.

A move is stored as its two effects — index the destination, drop the sources —
and committed in that order. That is `Indexer.move_file`'s ordering generalized
to the several sources a coalesced chain accumulates: a failure leaves every
source searchable, never a file that exists under no identity.

### Retry classification

`FileUpdateError.retryable` answers one question: would running the same
transaction again, unchanged, plausibly succeed?

| Failure | Retryable | Why |
| --- | --- | --- |
| Embedding provider outage | yes | The chunks are fine; the environment moved. |
| `FileUpdateCommitError` | yes | The chunk rows committed; re-running re-derives the vectors. |
| File saved mid-edit with a syntax error | no | A retry re-reads the same broken bytes. The fix is the developer's next save, which arrives as a new event with a fresh attempt budget. |
| Wrong-width or short embedding batch | no | A configuration mismatch, not a transient one. |
| `OSError` | yes | A file still being written, or a store briefly locked. |
| Anything unrecognized | no | Fails closed: reported, never retried blindly. |

Retries are bounded by `max_attempts` (3) with `retry_delays` backoff
(`0.5s`, `2.0s`), and the backoff is injectable so tests assert timing instead
of waiting it out.

A retry is a **replay**, which is the opposite of a fresh event: every pending
item is newer than it, so `requeue()` never overrides one. It contributes only
what nothing else owns — the drop obligations it was carrying, which `take()`
already removed from the pending set.

| Pending | Retry of | Result |
| --- | --- | --- |
| `index(b)` | `index(b)` dropping `a` | `index(b)` dropping `a` — the newer event re-reads the file, but `a` still has to go |
| `index(a)` | `index(b)` dropping `a` | `index(a)`, then `index(b)` dropping nothing — `a` is live again |
| `index(c)` dropping `b` | `index(b)` dropping `a` | `index(c)` dropping `b` and `a` — the replay is obsolete, its drops are not |

Discarding a superseded retry outright would leave a renamed-away file indexed
forever; letting one override a newer event would delete a file that exists.
Both were found by review before merge and are covered by named tests.

Terminal is never silent. Exhausted and terminal failures both land in
`BackgroundIndexer.failures()` as a bounded history of `WatchFailure` records,
each naming the path *and* the sources it never dropped — a failed rename
leaves its source in the index, so reporting only the destination would
under-report what is stale. Step 15 guarantees each failure left the file's
previous generation intact: the index is stale there, never damaged.

### Lifecycle and the drain contract

* `start()` returns whether it started the worker; a second call is a no-op.
  A stopped worker cannot be restarted — its queue rejects everything — so that
  raises rather than running a worker that can never commit.
* `stop(timeout)` closes the queue, drains what it can within one deadline for
  the whole call, and returns a `DrainReport`. `completed` is true only when
  every accepted item committed; `pending`, `in_flight`, and `failures` name
  everything else, and `incomplete_work` flattens them into the paths that are
  not in the index as their events implied.
* Retries continue while draining but skip their backoff, so shutdown never
  spends its budget sleeping.
* Work submitted after `stop()` raises `WatchQueueClosed`. `IndexingHandler`
  catches it and logs: an event arriving during shutdown must not kill the
  observer thread for every other file.

FastAPI is deliberately not wired to any of this yet; Step 17 owns the lifespan.

### One indexability rule

`Scanner.is_indexable(path)` answers, for one file, the question `scan()`
answers in bulk: extension (lowercased, as `scan()` compares it), inside the
watched root, and not ignored. `IndexingHandler` asks it for every event, so the
watched index and the built index contain the same files however a file arrived.

Containment is tested against the path as given first and its resolved form
second. `scan()` walks with `os.walk` and never resolves, so a symlink inside
the root is yielded even when its target is outside; resolving first would
reject exactly those files — the same watch/build disagreement in the other
direction. The resolved probe is the fallback, for an event carrying an alias
of the root itself (`/var/...` for a `/private/var/...` root).

## Server lifecycle ownership (Step 17)

Step 16 made the watch worker's queue lossless. Step 17 makes the *process*
that hosts it lossless: the server releases what it created, in one order,
within one deadline, and says what it could not finish.

Reproduced against the pre-Step-17 app through a real ASGI lifespan, with no
fault injection:

| After a complete lifespan (startup *and* shutdown) | Before | After |
| --- | --- | --- |
| Watch worker thread | still running | stopped |
| Watchdog observer | still alive | stopped and joined |
| SQLite chunk repository | still open | closed |
| `api._service` | still installed | uninstalled |
| Two edits queued at shutdown | 0 chunks committed, nothing reported | both committed, or both named in the report |
| Watched vectors, FAISS backend | 1 in memory, 0 on disk | persisted by the flush |

### One creator, one closer

| Resource | Created by | Closed by |
| --- | --- | --- |
| Knowledge store, chunk repository, vector store | `KnowCodeService` | `KnowCodeService.close()` |
| Background worker, file monitor | `ServerResources.startup()` | `ServerResources.shutdown()` |
| The service itself | `create_app` | the app's lifespan, through `ServerResources` |
| Telemetry writer pool | `knowcode.telemetry`, on first use | `shutdown_telemetry()` |

The watch resources start in the ASGI **lifespan**, not in `create_app`. An app
that is built but never run therefore owns no threads, and every app that *is*
run is guaranteed a matching teardown.

### The close order

`SHUTDOWN_ORDER` is `monitor → worker → flush → stores → telemetry`, and every
shutdown reports all five stages, including the ones with nothing to do:

1. **monitor** — stop the observer first, so the drain works against an inbox
   nothing is still filling.
2. **worker** — `BackgroundIndexer.stop()` within the remaining budget. Its
   Step 16 `DrainReport` decides this stage: `completed` passes it, anything
   else fails it and contributes `incomplete_work`.
3. **flush** — `KnowCodeService.flush()`: drain the vector write buffer, then
   persist the vector artifact and index manifest.
4. **stores** — `KnowCodeService.close()`: chunk repository, then knowledge
   store. Both drain in-flight readers before closing (Step 09).
5. **telemetry** — last, because the stages above still log.

A stage that fails does **not** skip the stages after it. Losing buffered data
and then also leaking the connection is strictly worse than losing the data, so
failures are recorded and teardown continues.

The whole call shares one deadline, so a commit that hangs cannot hold the
process open. Both `startup()` and `shutdown()` are idempotent; a shutdown
returns its first report rather than closing anything twice, and a failed
startup rolls its worker back so a half-wired server leaves no consumer running.

### Never claim unearned durability

`ShutdownReport` is structured, not prose: `completed`, `failed_stages`,
`incomplete_work`, and a per-stage outcome with the error text. `completed` is
true only when every stage succeeded *and* the worker drained everything it
accepted. `incomplete_work` carries repository paths — the same surface the
watch worker already exposes — and no query text, credential, or config value
passes through it.

### What `flush` will and will not write

Chunks are durable as they commit (Step 15), but vectors are not, and the two
backends differ: LanceDB buffers writes, while FAISS/NumPy keeps the whole index
in memory and writes it only in `Indexer.save`. A watch session with the FAISS
backend therefore ended with vectors in memory and none on disk — durable chunks
beside no vectors, which is this plan's split-brain reappearing at the process
boundary. `flush()` drains the buffer and writes the artifact.

Two cases are deliberately skipped, and reported rather than performed:

* **A published generation.** ADR 4 makes it immutable and records artifact
  checksums at publication, so rewriting `vectors.*` or `index_manifest.json`
  inside it would invalidate the manifest describing it. Since Step 18b nothing
  is buffered there either: watch commits stage a successor generation, and
  `flush()` publishes it (see below).
* **An index that never existed.** An empty store with no artifact beside it has
  nothing to persist. An empty store *with* one is still written — that records
  that everything was removed.

### Telemetry as an owned resource

The writer pool was a module-level `ThreadPoolExecutor` created at import and
never shut down, so a queued write could still be in the pool at process exit.
It is now created on first use and released by `shutdown_telemetry(timeout)`,
which returns whether every accepted write finished. The pool is re-created on
demand, so one server's shutdown never breaks the next one's logging in the same
process.

Concurrent *reload* — handing in-flight readers from one generation to the next,
and closing retired resources behind them — is not here. That is the generation
bundle below.

## Generation bundles and reader leases (Step 18)

ADR 4 ends with a reader contract that Steps 14 and 15 could not yet honour:
"readers acquire one generation lease and use it for sparse, dense, graph, and
context work. Superseded generations are retired only after reader leases end."
Until Step 18 the service kept the knowledge store, the indexer, and the search
engine in three independent, unlocked lazy fields, which produced three distinct
failures:

* Four threads racing first use opened four `SqliteKnowledgeStore` instances,
  three of which leaked.
* `reload()` replaced those fields one at a time, so an operation already under
  way could answer its graph half from one generation and its dense half from
  the next.
* Retired stores were dropped without being closed — closing them would have
  torn a connection out from under whichever request still held it — leaking a
  writer and a reader connection per reload.

### One bundle per generation

`GenerationBundle` is the complete, immutable reader set for exactly one
published generation: its knowledge store, its indexer (chunk repository plus
vector store), and the search engine derived from both.

* **Lazy but single-flight.** A bundle nobody reads opens no connection; a
  bundle two threads read opens each component once. A failed open caches
  nothing, so the next caller retries against a whole bundle rather than a
  half-built one.
* **Immutable membership.** Components are never replaced inside a bundle.
  Moving to a newer generation means building a *new* bundle.

### The lease

`KnowCodeService.generation_lease()` pins one bundle for a whole operation.
While it is held, `store`, `get_indexer()`, and `get_search_engine()` all
resolve to that bundle — including inside nested service calls, because the
lease lives in a `ContextVar` keyed by service instance rather than being
threaded through every signature. Nesting reuses the lease already held rather
than taking a second one.

Every service method that makes more than one store call takes a lease
internally (`search`, `get_context`, `get_stats`, `get_callers`, `get_callees`,
`get_entity_details`, `retrieve_context_for_query`), and the API's query
endpoint takes one around retrieval *and* the chunk resolution that follows it.

### Swap and retirement

`reload()` builds its replacement bundle off to the side and warms it to
whatever the outgoing bundle had already opened. Only a bundle that opened
successfully is installed, and it is installed whole, under a narrow lock. A
generation that cannot be loaded is rejected while the current one keeps
serving — before Step 18 the same situation left the service with no store at
all, so one failed refresh permanently broke a working service.

The replaced bundle is then *retired*: it refuses new leases and closes when its
last reader releases. Retirement runs outside the swap lock, because closing a
SQLite store waits for that store's own in-flight readers to drain (Step 09).

`close()` on the vector stores is the member this made safe to require; it is
now part of `VectorStoreProtocol`. Both backends drain buffered writes first, so
a write that already returned is never lost by the close that follows it, and a
closed store is empty rather than poisoned.

### Directory retention

Closing a bundle is not enough on its own: retention deletes superseded
generation *directories*, and one of them may be the directory a request is
reading out of. `publish_generation(..., protect=...)` takes the ids the service
reports through `live_generation_ids()` — the current bundle plus every retired
bundle still leased — and retention skips them. A protected generation is simply
removed by a later publication, once nobody holds it.

This protection is process state, so it covers publications made by the serving
process, which is what ADR 4's one-writer-process-per-artifact-root rule
assumes. A publication from a separate process applies plain retention; a reader
holding open handles keeps answering, but the directory can disappear beneath
it.

### The watch worker

The worker used to be constructed with one `service.get_indexer()` result and
hold it for the process's lifetime, so after a reload it kept committing into
the generation the service had moved off — and, once retirement started closing
things, into a closed repository. It is now given a `ServiceWatchWriter`, which
resolves the current bundle under a lease per commit. A reload racing a commit
therefore cannot close a repository mid-transaction; the swap takes effect from
the next commit.

### Deferred: where watch commits publish

Step 18 fixed *which* generation a commit reached, not *what a commit is allowed
to write*: the worker still committed into the current generation's artifacts in
place. That is Step 18b, below.

## Watch commits as generations (Step 18b)

Step 18b closes the last place an index mutation was not a generation. A watch
commit resolved the current bundle and wrote into the published generation's own
`chunks.db` and vector artifacts, which ADR 4 makes immutable and whose manifest
records digests over exactly those files.

Reproduced against the pre-Step-18b service, with no fault injection: after one
watched edit, `validate_generation(..., verify_digests=True)` on the current
generation reported a chunk-id digest mismatch and a chunk-count mismatch, and
`knowcode doctor` failed on it. With FAISS/NumPy the edit was not durable at all
— `flush()` correctly refuses to rewrite a published generation — so a restart
found two chunks beside one vector. On LanceDB the table *did* change underneath
its own envelope, so the restart failed closed with
`VectorArtifactVersionError`.

### Stage, then publish

`StagedGenerationWriter` copies a published generation into
`.staging-<id>.pid<pid>/`, applies Step 15 file transactions there, and
publishes the result through the same validate-then-move-the-pointer path a
build uses. `knowledge.db` is carried across unchanged, because a file
transaction rewrites chunks and vectors and never the graph. The published
original is read once and never written.

The copy is a full one. Hardlinking would let a SQLite write in the successor
reach the published file it was linked to, which is the defect staging exists to
prevent — the same reason Step 15 accepted a full copy for incremental builds.

Seeding fails closed. A base directory that is gone copies *nothing*, and every
store opened on the staging directory afterwards would helpfully create an empty
one; publishing that would replace a populated generation with an empty one and
call it a successor.

### Cadence: per drain, capped per batch

Publication is not per commit — that would cost a full staging copy per keypress
— and not per session, which would make a watch session's work visible only at
shutdown. The worker publishes when its **queue goes idle**, before it releases
the last item, so a caller whose `join()` returns can rely on the batch having
been published rather than racing it. One burst of saves is therefore one
staging copy and one publication, and a single save is visible as soon as it is
indexed.

`DEFAULT_WATCH_BATCH_COMMITS` (64) publishes mid-burst as well. That bounds two
things at once during a branch switch or a bulk format: how stale a reader can
get while the burst runs, and how much committed work a crash would leave in a
staging directory nobody publishes.

**The staleness readers accept** is therefore one drain: between a commit and its
publication, readers keep answering from the previous generation — completely,
not partially — and move to the new one in a single atomic swap (Step 18).

### Rebased, never reverted

A staged batch is a successor to the generation it was seeded from, so
`publish_generation(..., expect_current=...)` compares the pointer under the
publication lock and raises `GenerationConflictError` rather than reverting a
build that landed in between. The writer then re-derives the batch — re-applying
its recorded file operations to a new staging generation seeded from the new
current one — and publishes that. Re-derivation rather than replay of the staged
*artifacts*: the batch means "these files' current content belongs in the
index", which applied to the new base preserves both changes.

One re-derivation, not unbounded retries. A second conflict means sustained
contention, and re-deriving costs a re-parse and re-embed of the batch. The
batch is retained instead, so the next publication — the worker's next drain —
tries again.

### Committed is not durable

Work that is committed into a staging generation and not published is named by
`ServiceWatchWriter.pending_paths()`, which `DrainReport.unpublished` carries
into `incomplete_work` and from there into the server's `ShutdownReport`. A
drain that could not publish is not `completed`.

`flush()` is a durability boundary again in watch mode: it publishes whatever is
still staged, which is what makes Step 17's shutdown order — drain the worker,
*then* flush, then close the stores — end with the watch session's work in a
published generation. `close()` deliberately does not publish; it discards the
staging directory and logs the lost identities by name.

A published generation is still never rewritten, and now nothing is buffered
inside one: watch commits no longer land there at all.

### Where commits still land in place

An artifact root with no published generation — the pre-Step-14 flat layout, and
an index nobody has built yet — is genuinely mutable. There is nothing immutable
to protect and `flush()` already makes it durable, so commits go straight into
it and `publish_pending()` reports that there was nothing to publish.

## Prompt hierarchy and untrusted context (Step 19)

Before Step 19 the task instructions, the retrieved repository context, and the
user's question were concatenated into one string and sent as a single user
turn to both provider families. Any indexed file could therefore place text at
the same hierarchy level as KnowCode's own instructions, and there was no
system-instruction channel in the request at all.

`knowcode.llm.prompt_contract` now owns the split. It is the only place a
provider request body is constructed; `Agent.answer` selects a provider and
hands it the two channels.

### Two channels, never one string

The instruction channel carries KnowCode-authored text only: the task template
from `query_classifier.TASK_PROMPTS` plus the fixed `UNTRUSTED_DATA_POLICY`. No
question and no retrieved content is ever interpolated into it.

| Provider family | Instruction channel | Data channel |
| --- | --- | --- |
| `google` | `config.system_instruction` | `contents=[payload]` |
| OpenAI-compatible (`openai`, `openrouter`, `mistralai`, `glm`, `z-ai`, …) | `messages[0]` with role `system` | `messages[1]` with role `user` |

### The untrusted-input envelope

The question and the retrieved context are serialized together as one JSON
object, version `knowcode_untrusted_input_version: 1`:

```json
{"knowcode_untrusted_input_version": 1,
 "question": {"chars": 11, "truncated": false, "original_chars": 11, "text": "Explain foo"},
 "repository_context": {"chars": 14, "truncated": false, "original_chars": 14, "text": "def foo(): ..."}}
```

JSON escaping is the boundary, not a sentinel string. A retrieved comment
cannot close the string it lives in, and every control character — newlines
included — is escaped, so the entire user turn is one physical line no matter
what the repository contains. A forged envelope inside `text` stays a string
value; the `chars` and `truncated` fields let the model tell the real envelope
from repository content that imitates one, and the system instruction says so
explicitly.

Repository content is declared evidence, never instruction: text inside it that
reads as a system prompt, a role marker, a new task, or a request to reveal
other context is to be reported as a finding in the code rather than obeyed,
and answers must stay grounded in the supplied evidence.

### Bounds

`MAX_QUESTION_CHARS` (8,000) and `MAX_CONTEXT_CHARS` (120,000) bound the two
fields at construction time, behind retrieval's own token budget. Truncation is
declared in the payload — `truncated: true` with the pre-truncation
`original_chars` — rather than applied silently.

### Logging

KnowCode never prints either channel. Provider failures print
`format_provider_error(exc)`: the exception type plus a whitespace-collapsed
message bounded to `MAX_PROVIDER_ERROR_CHARS` (200). This bounds — and does not
eliminate — the case where a provider quotes the request back in its own error
text; up to that many characters of a quoting provider's message still reach
the operator's own console. Telemetry field policy stays with Step 20.

### Not claimed

These are construction guarantees. A model's compliance with the instruction
hierarchy is probabilistic and is not asserted anywhere in the test suite; the
tests assert where each piece of text is placed in the request.

## Baseline evidence

The baseline is `main` at `d239b22` on 2026-08-12. Before Step 01 additions,
the focused suite was:

```text
uv run pytest -q tests/unit/parsers tests/unit/indexing
144 passed, 3 warnings in 0.58s
```

The warnings were FAISS SWIG deprecations from
`test_indexer_writes_and_loads_manifest`.

Step 01 followed a red/green sequence. The initial identity test failed to
import `EndpointKind`; the initial parser contract test failed to import
`tests.helpers.parser_assertions`. After the minimum implementations, the
focused identity and fixture-helper tests passed.

### Parser reproductions

This command compares every committed target fixture with the corresponding
baseline parser without writing generated artifacts:

```bash
uv run python - <<'PY'
from pathlib import Path

from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_parser import VueParser
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    load_parser_fixture_contract,
)

root = Path("tests/fixtures/parser_contracts")
parsers = {
    "javascript": JavaScriptParser(),
    "python": PythonParser(),
    "rust": RustParser(),
    "typescript": TypeScriptParser(),
    "vue": VueParser(),
}
for source in sorted(root.rglob("*")):
    if not source.is_file() or source.name.endswith(".expected.json"):
        continue
    contract = load_parser_fixture_contract(source)
    try:
        assert_exact_parse_result(
            parsers[contract.language].parse_file(contract.source_path),
            contract,
        )
    except AssertionError as error:
        print(f"{source}: {error}")
PY
```

The baseline observations are stable: JavaScript omits both inheritance edges;
TypeScript emits only its module; Python omits nested definitions and module
variables, starts decorated entities after decorators, and leaks nested calls
to outer scopes; Vue misses attribute-bearing sections or assigns script-tag
locations and fabricates method/data targets; Rust emits `type::`/`trait::`
endpoints and duplicate structural parents.

### Storage, isolation, and injection reproductions

The following temporary-store commands were executed against the baseline:

```bash
uv run python - <<'PY'
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from knowcode.data_models import CodeChunk
from knowcode.storage.lancedb_vector_store import LanceDBVectorStore
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

vectors = VectorStore(2)
vectors.add("dead", [1.0, 0.0])
vectors.add("live", [0.9, 0.1])
vectors.remove("dead")
print(vectors.count(), vectors.index.ntotal, vectors.search([1.0, 0.0], 1))
# 1 2 []

with TemporaryDirectory() as tmp:
    repo = SqliteChunkRepository(Path(tmp) / "chunks.db")
    repo._conn.execute("BEGIN")
    repo._conn.execute(
        "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
        "metadata_json, file_path) VALUES (?, ?, ?, ?, ?, ?)",
        ("dirty", "file.py::value", "secret", "secret", "{}", "file.py"),
    )
    observed = []
    reader = threading.Thread(target=lambda: observed.append(repo.get("dirty") is not None))
    reader.start()
    reader.join(timeout=2)
    print(observed)
    # [True]
    repo._conn.rollback()
    repo.close()

with TemporaryDirectory() as tmp:
    store = LanceDBVectorStore(2, Path(tmp) / "vectors")
    hostile = "x' OR true --"
    store.add("safe", [0.0, 1.0])
    store.add(hostile, [1.0, 0.0])
    store.search([1.0, 0.0], 2)
    print(store.get_embedding(hostile), store.count())
    store.remove(hostile)
    print(store.count())
    # hostile read returned the safe vector; counts changed 2 -> 0
PY
```

The incremental split-brain reproduction uses a normalized temporary root,
indexes one Python chunk, then calls `Indexer.remove_file()`. Counts change
from `(chunks=1, vectors=1)` to `(chunks=0, vectors=1)`, and dense search still
returns the deleted chunk ID. Passing the unresolved macOS `/var/...` alias
also leaves the chunk row in place, independently proving the path-identity
defect.

```bash
uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from knowcode.data_models import EmbeddingConfig
from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

class Embedder:
    config = EmbeddingConfig(provider="test", model_name="test", dimension=2)
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]
    def embed_single(self, text):
        return [1.0, 0.0]

with TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    source = root / "sample.py"
    source.write_text("def searchable():\n    return 1\n")
    chunks = SqliteChunkRepository(root / "chunks.db")
    vectors = VectorStore(2)
    indexer = Indexer(Embedder(), chunks, vectors)
    indexer.index_file(source)
    print(chunks.count(), vectors.count())
    indexer.remove_file(source)
    print(chunks.count(), vectors.count(), vectors.search([1.0, 0.0], 10))
    chunks.close()
# 1 1
# 0 1 [(<deleted chunk id>, 1.0)]
PY
```

### Privacy, prompt, and proxy reproductions

```bash
KNOWCODE_TESTING=1 uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from knowcode.telemetry import log_event

with TemporaryDirectory() as tmp:
    log_event(tmp, {"query": "token=sk-secret-value"})
    payload = (Path(tmp) / "knowcode_telemetry.jsonl").read_text()
    print("sk-secret-value" in payload)
    # True
PY

rg -n 'prompt = f|contents=prompt|messages=' src/knowcode/llm/agent.py
# Shows instructions, retrieved context, and question concatenated into one prompt.

uv run python - <<'PY'
import asyncio
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

seen = {}
async def app(scope, receive, send):
    seen["client"] = scope["client"]
async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}
async def send(message):
    pass

scope = {
    "type": "http",
    "scheme": "http",
    "server": ("127.0.0.1", 8000),
    "client": ("127.0.0.1", 50000),
    "headers": [(b"x-forwarded-for", b"203.0.113.77")],
}
middleware = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1"])
asyncio.run(middleware(scope, receive, send))
print(seen)
# {'client': ('203.0.113.77', 0)}
PY
```

Step 21 will replace the middleware-level proxy reproduction with an enabled
limiter test through the real server socket.
