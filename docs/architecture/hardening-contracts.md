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
