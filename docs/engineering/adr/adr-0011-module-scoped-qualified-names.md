# ADR 0011: A file's module is the outermost lexical scope

**Status:** Accepted

**Date:** 2026-08-31

**Amends:** [ADR 0001](adr-0001-entity-and-file-identity.md) (which scope a
qualified name starts from, not the id grammar)

**Origin:** BL-9 in the [engineering backlog](../backlog.md)

---

### Context

ADR 0001 says a qualified name includes lexical scope and gives four examples,
one of which is `module.Type.method`. It does not say whether `module` there
means a file or an explicit module construct, and the parsers split on the
question. `RustParser` read it as the file and emitted `sample.top`,
`sample.S.meth`, `sample.inner.deep`. `ProseChunker`'s `HeadingScope` read it
the same way and roots every heading under the document name, which is how BL-1
was closed. `PythonParser`, `JavaScriptParser`, `TypeScriptParser` and
`JavaParser` read it as an explicit construct and gave a top-level declaration
the bare name.

Every parser also emits a synthetic entity standing for the file, with the file
stem as its qualified name. Under the bare-name reading a top-level symbol
sharing the stem mints exactly that id, so the two entities have one id and one
silently replaces the other in `knowledge.db`.

Measured on this repository: 3 of 335 indexable tracked files. In
`src/knowcode/cli/cli.py` the module entity spanning lines 1 to 1071 and the
five-line `cli()` function both claimed `…/cli.py::cli`. The declaration won
the merge, so **48 edges that belonged to the module, 27 `imports` and 21
`contains`, came to rest on a five-line function**, along with the file's whole
import block as a chunk. A retrieval answer about `cli()` carried the file's
entire import surface as though the function owned it.

### Decision

A file's module is the outermost lexical scope in that file. Every declaration
made directly in a file carries the module's qualified name as its prefix, and
the module entity keeps the bare stem.

```text
mod.py    module   mod
          function mod.alpha
          class    mod.Cls
          method   mod.Cls.meth
```

`TreeSitterParser._module_scope` is the single definition of that prefix, used
by the tree-sitter parsers and by `PythonParser`'s root scope. A parser that
composes a child name from a parent must use the parent's *qualified* name, not
its bare one, or the prefix stops at the first nesting level.

Rejected: giving the synthetic module entity a reserved qualified name such as
`::module::<stem>`. ADR 0001 rules out ad hoc internal namespaces, and BL-6 is
the evidence that inventing one produces ids nothing else agrees with. The
collision is not the module's fault; it is that a declaration was claiming a
scope it does not have.

### Consequences

Every entity id in every language changes, so `Indexer.SCHEMA_VERSION` moves
and every generation rebuilds. Qualified names shown to a reader and to the
model gain one segment, which is the honest reading: `alpha` alone never
identified anything.

Reference resolution is unaffected. `GraphBuilder._find_entity_by_name` matches
`Entity.name` first, and `name` is unchanged. A parser's own local symbol table
is affected, and must hold the same scoped names its entities carry; a bare
name there resolves a reference to an id no entity was emitted under.

Vue is the remaining exception. `VueParser` emits no entity for the file at all,
so its declarations root under the component name instead. That is BL-10.
