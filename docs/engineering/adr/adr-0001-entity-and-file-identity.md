# ADR 0001: Entity and file identity

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

**Amended by:** [ADR 0010](adr-0010-root-relative-id-storage.md) (the stored
form of an id) and [ADR 0011](adr-0011-module-scoped-qualified-names.md) (which
scope a qualified name starts from)

---


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

