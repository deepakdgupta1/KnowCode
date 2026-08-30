# ADR 0010: Ids are stored relative to a recorded repository root

**Status:** Accepted

**Date:** 2026-08-29

**Amends:** [ADR 0001](adr-0001-entity-and-file-identity.md) (the storage form
of an entity id, not its identity rule)

**Origin:** `docs/research/storage_optimization_2026_v4.md` Phase C1

---

### Context

ADR 0001 makes an entity id the absolute POSIX path of its file, joined to a
lexical qualified name. It chose absolute ids on purpose, to "avoid a
cross-cutting root parameter in every parser", and accepted that the result is
machine-local.

That choice has a cost nobody had measured. Every id column repeats the
repository root once per row, so the size of a published generation depends on
where the build ran. Two builds of this repository from the same
`git archive HEAD` extraction, differing only in the path they were extracted
to, produced:

| | 29-character root | 134-character root |
|---|---:|---:|
| `knowledge.db` | 15,167,488 | 21,000,192 |
| `chunks.db` | 44,494,848 | 50,053,120 |
| generation | 59,662,336 | 71,053,312 |

The deeper root cost 10.9 MB, 19% of the generation, on byte-identical
content. A continuous integration checkout is usually deeper than a developer's
home directory, so the artifact a team publishes is the expensive one.

Phase C2 made this worse before it made it visible. Interning edge endpoints
into the `eid` codebook moved 17,024 endpoint ids into a table of their own,
and that table plus its unique index became the largest single holder of
repeated root text in `knowledge.db`.

The obvious repair is to make ids repo-relative everywhere: in the parsers, in
`GraphBuilder`, in retrieval. That is the change
`storage_optimization_2026_v4.md` §11 describes, and it reverses ADR 0001's
reasoning, inverts the `Path(file_identity).is_absolute()` test that
`classify_endpoint_id` uses to recognise an internal endpoint, and touches
every parser.

### Decision

An id is absolute everywhere above the storage layer. `SqliteChunkRepository`
and `SqliteKnowledgeStore` store it relative to a repository root they record,
and restore the root on the way out. No caller observes the stored form, the
same way no caller observes the integer edge keys that Phase C2 introduced.

Each database records its root in a one-row `repo_root` table. `set_repo_root`
binds it once, before the first write. `Indexer.index_directory` binds the
chunk repository, and `KnowCodeService._write_staged_knowledge_store` binds the
knowledge store.

Two functions in `knowcode/utils/entity_identity.py` own the encoding.
`relativize_id` strips the root, and `absolutize_id` restores it. They handle
the three endpoint shapes ADR 0001 defines. An internal id carries its path at
the front. An `unresolved::` id carries a percent-encoded path in its third
component, so the codec encodes the root the same way before matching it. An
`external::` id carries no path and passes through.

The pair is total, because a stored component is relative exactly when it is
not absolute. `relativize_id` strips only a prefix it recognises, so a file
outside the root keeps its absolute path, and `absolutize_id` prepends the root
only to a component that `Path.is_absolute` rejects. A path the encoder left
alone is therefore a path the decoder also leaves alone. `relativize_id` is
idempotent, so applying it twice is safe.

A generation opened against a different root resolves its ids against the root
recorded in the database, not the one it is opened at. Behavior is unchanged
from before this ADR: moving a repository still requires a rebuild, as ADR 0001
says. Rebinding a populated database to a different root raises instead of
silently re-anchoring the rows written under the old one.

`read_entity_ids` and `read_chunk_ids` in `knowcode/indexing/generations.py`
read ids by raw SQL, so they hydrate through the recorded root too. A manifest
therefore digests ids in the absolute form callers hold, and the digest does not
depend on how the artifact encodes them.

`Indexer.SCHEMA_VERSION` goes to 5. A reader that predates the codec would hand
callers relative ids and resolve nothing, so the version refuses the artifact
rather than serving broken paths.

### What this does not change

ADR 0001 still owns identity. An entity id is still the absolute resolved POSIX
path joined to a lexical qualified name, symlinks are still identity aliases,
and the three endpoint namespaces are unchanged. `classify_endpoint_id` still
requires an absolute path for an internal endpoint, because it only ever sees
hydrated ids.

### Consequences

Measured by rebuilding both extractions above with the codec in place:

| | 29-character root | 134-character root |
|---|---:|---:|
| `knowledge.db` | 13,459,456 | 13,459,456 |
| `chunks.db` | 42,950,656 | 43,114,496 |
| generation | 56,410,112 | 56,573,952 |

The generation is 3.10 MB smaller at the shallow root and 13.81 MB smaller at
the deep one. `knowledge.db` is now byte-identical between the two, and the
whole generation differs by 163,840 bytes, down from 11,390,976. What remains
is `metadata_json`, which holds a path in 1,017 chunk rows and is data rather
than identity, so the codec leaves it alone.

The change is lossless, and the manifest proves it. Rebuilding the shallow
extraction produced the same entity-id digest, the same chunk-id digest, and
the same counts as the build before the change:

```text
entity_ids  sha256:e79aa81bd73f45263622a20f3b4d150c275fd61892d61f71535d834f061e23dc
chunk_ids   sha256:4f2ba721652fca0970f2125b027383bd01ff3407ed29bf59c999d5beb209eec0
counts      entities 5350, relationships 23858, chunks 6620, vectors 6620
```

Every id column in the published artifact is now relative, including all 17,217
rows of the `eid` codebook, and all three endpoint kinds round-trip.

The cost is that two stores now hold a piece of state they did not before, and
a write path that forgets to bind the root silently stores absolute ids. That
failure is invisible in retrieval, because the codec is total and absolute ids
read back unchanged. It shows up only as an artifact that is larger than it
should be, which is why the size comparison between two roots is the check
worth keeping.
