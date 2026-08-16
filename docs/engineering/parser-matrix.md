# Parser Construct Support Matrix

**Status:** Step 07 snapshot of the hardening blueprint

**Scope:** What each KnowCode parser extracts today, and the explicit
limitations a user or downstream step can rely on. This matrix is enforced by
the cross-language integrity gates in `tests/unit/indexing/` and
`tests/unit/parsers/`, and the exact fixture contracts in
`tests/fixtures/parser_contracts/`. It is the single
source of truth referenced by the release gate (blueprint Step 22).

The parser and graph invariants themselves live in
[ADR 1](adr/adr-0001-entity-and-file-identity.md). Anything not listed
as supported here is either an explicit limitation below or outside the
committed fixtures.

## How to read this matrix

- **Supported** means a committed fixture or gate proves exact extraction,
  location, and graph-identity behavior through `GraphBuilder`, not only direct
  parser output.
- **Limitation** means the construct is either not extracted or extracted
  incompletely, and the gap is *visible* (reported through `ParseResult.errors`
  or documented here) rather than silently lost. Silent loss is a defect, not a
  limitation.

## Endpoint and identity contract (all languages)

Every relationship endpoint is exactly one of:

| Classification | Form |
| --- | --- |
| Internal | `<canonical-file>::<qualified-name>` |
| External | `external::<namespace>::<symbol>` |
| Unresolved | `unresolved::<language>::<file>::<scope>::<symbol>` |

Legacy pseudo-namespaces (`type::`, `trait::`, `::method::`, `::data::`) are
invalid and rejected by the gates. A parser may emit a transient `ref::<name>`
placeholder for a reference it cannot fully qualify at parse time;
`GraphBuilder._resolve_references` links it to a local entity when one exists.
Committed fixtures and the mixed-language merge contain no lingering `ref::` or
invalid endpoints.

**Scoping note:** this endpoint contract is enforced for the hardened parsers
above (JS/TS, Python, Vue, Rust) and the mixed-language gate fixture. The Java
parser predates it and still emits non-canonical forms — see its limitations
below; the mixed-language fixture contains no `.java` file, so the gates do
not cover it.

Duplicate declarations in one file never produce duplicate entity IDs: the
parser keeps the first and reports the dropped collision (Step 07). The
synthetic per-file *module* entity (named after the file stem) is intentionally
excluded from that dedupe so a top-level declaration whose name matches the file
stem — notably a Java `public class` in `ClassName.java` — remains a redundant
wrapper that the graph merge resolves in favor of the declaration.

## JavaScript and TypeScript (tree-sitter)

**Supported:**

- Classes, functions, arrow functions, and function-valued variable
  declarations.
- TypeScript interfaces, type aliases, and enums (extracted as `class` entities
  until the shared entity-kind schema expands).
- Named and default `export` declarations, unwrapped through one shared
  dispatch path.
- `extends` for simple identifiers, member expressions, and the explicitly
  supported complex grammar forms; nonlocal bases become unresolved references.
- Module entity, containment, and call edges.

**Limitations:**

- `.tsx` **JSX bodies** are reported as tree-sitter syntax errors, because
  `TypeScriptParser` uses the plain `typescript` grammar (no TSX); tree-sitter
  error recovery still yields partial extraction, but the entity set is not
  guaranteed beyond the module entity. `.jsx` files route to
  `JavaScriptParser`, whose `javascript` grammar accepts JSX and extracts
  normally — JSX *tags* are not extracted as entities in either case.
  JSX-free TypeScript in a `.tsx` file parses and extracts normally.
- TypeScript interfaces, aliases, and enums share the `class` entity kind.
- Duplicate top-level declarations are deduped and reported (Step 07); the
  synthetic module entity is exempt so a Java-style filename match does not
  false-trigger.

## Python (`ast`)

**Supported:**

- Nested classes, nested functions, async nested functions, with lexical
  qualified names (`Outer.Inner`, `Outer.method.local`).
- Decorators on classes, functions, and methods; entity location and source
  begin at the first decorator.
- Module `Assign` and `AnnAssign` produce one variable entity per simple bound
  name, including each name in chained or tuple assignment.
- Scoped call resolution: a call resolves to the lexical scope that owns it and
  never leaks across nested definition boundaries.
- Imports (external) and inheritance.

**Limitations:**

- A syntax error fails the **whole file** (`ast.parse` raises); there is no
  partial extraction. The failure is visible in `ParseResult.errors` and
  deterministic.
- Module-local (non-module-scope) assignments are intentionally not entities.

## Vue SFC (custom parser)

**Supported:**

- Attribute-order-insensitive SFC section scanning with exact byte/line offsets;
  malformed or unclosed sections are reported.
- Composition API (`<script setup>`) and Options API; script content parsed with
  the JS or TS parser and rebased into the `.vue` file.
- Template bindings (`v-model`, `@event` handlers), emitted events, CSS
  `v-bind()`, imports, and composables, resolved through a per-component symbol
  table. Template `v-bind:` / `:` directives are **not** extracted — only the
  CSS `v-bind()` form in `<style>` is.
- Exact declaration lines and source snippets.

**Limitations:**

- `v-model.trim` and other `.modifier` forms drop the binding edge.
- Quoted `data()` keys (`"count": 0`) and array-form `defineProps(['title'])`
  produce no entity.
- Generic `defineEmits<{ (e: 'save'); (e: 'cancel') }>()` captures only the
  first event.
- An `import` statement inside a block comment still registers.
- `_get_component_name` lowercases interior capitals, so `MyButton.vue` yields
  the entity `Mybutton` while importers yield `external::vue_component::MyButton`
  — this blocks future cross-file resolution until normalized.
- A component pairing a plain `<script>` with `<script setup>` indexes only the
  setup block; the plain block's Options API declarations are missing and
  reported.
- Duplicate Options API method keys or `data()` keys silently keep the first
  declaration (not reported); template-binding name collisions *are* reported.
- Relationship identity is `(source, target, kind, binding_type)`; the shared
  fixture helper keys on `(source, target, kind)`, so two legitimate edges that
  differ only by `binding_type` are both retained but cannot yet be expressed in
  one fixture.

## Rust (tree-sitter)

**Supported:**

- Structs, enums, traits, inherent and trait `impl` blocks, methods, generics,
  and qualified trait paths.
- Lexical module-scope reference resolution: a bare name declared once resolves
  to its entity; qualified or foreign paths become scoped unresolved references.
- Calls for local functions and `Type::method` naming a same-scope method;
  imports from `use` trees (including `self`, grouped, and `as` forms).

**Limitations:**

- `const`, `static`, and type aliases are extracted with no containment edge, so
  they are unreachable by graph traversal.
- Trait bodies contribute no entities; default methods and required signatures
  are missing.
- Associated `const` / `type` items inside `impl` blocks are skipped.
- Import-aware external trait classification is not performed, so a trait
  reached through an imported path (`use std::fmt;` then `impl fmt::Display`)
  is unresolved rather than external.
- Structs, enums, traits, and type aliases share the `class` entity kind.
- A field and a method on the same type can collide on the `Type.name`
  qualified-name scheme; the collision is dropped and reported.

## Java (tree-sitter)

**Supported:**

- Classes and interfaces (both `class` entities), methods and constructors
  (both `method` entities), with containment edges from the file module.
- Imports as `IMPORTS` edges; `extends`/`implements` superclasses as
  `INHERITS` edges via `ref::` placeholders resolved against local entities by
  `GraphBuilder`.
- Calls: method invocations (`foo.bar(...)`, bare `bar(...)`) and
  `new Foo()` constructor calls, emitted as `CALLS` edges via `ref::`
  placeholders.

**Limitations (pre-hardening parser; not covered by the endpoint gates):**

- Endpoint forms predate [ADR 1](adr/adr-0001-entity-and-file-identity.md):
  imports emit `external::<dotted.name>` (e.g. `external::java.util.List`)
  instead of `external::<namespace>::<symbol>`, and internal IDs are raw
  `file::name` strings without the shared normalization helpers, so
  `classify_endpoint_id` marks them invalid.
- `ref::` placeholders without a matching local entity linger in the graph
  instead of becoming `unresolved::` endpoints.
- Nested method invocations can emit duplicate `CALLS` edges (the cursor walk
  re-visits a parent invocation after its children).
- Class fields, enum declarations, and annotation declarations are not
  extracted; `package` names are not part of qualified names.

## Markdown (custom parser)

**Supported:**

- One `DOCUMENT` entity per file (first paragraph as docstring) and one
  `SECTION` entity per ATX heading (`#`–`######`), with `CONTAINS`
  relationships following heading nesting, heading levels in metadata, and
  exact section line ranges.
- Headings inside fenced code blocks are deliberately skipped.

**Limitations:**

- Setext headings (underlined with `===` / `---`) are not detected — only
  ATX `#` headings are.
- Section IDs are slugified titles; duplicate heading titles collide on one
  ID (the first wins after graph dedupe).

## reStructuredText (custom parser)

**Supported:**

- Mirrors the Markdown parser: one `DOCUMENT` entity plus one `SECTION`
  entity per heading with a `CONTAINS` hierarchy. Heading detection uses the
  shared RST adornment scanner (`prose_chunker._scan_rst_headings`), so
  section levels stay consistent between the graph and the retrieval index.

**Limitations:**

- Inherits the Markdown shape: no cross-references, directives, or role
  markup; only section structure is extracted.

## YAML (custom parser)

**Supported:**

- One `DOCUMENT` entity per file plus one `CONFIG_KEY` entity per mapping
  key at any nesting depth (dotted qualified names), with `CONTAINS`
  relationships following key nesting, value type and a bounded value preview
  in metadata. `yaml.safe_load` with parse errors visible and deterministic.

**Limitations:**

- Keys only: scalar/list values contribute a preview, never entities; a
  non-mapping root (list or scalar) yields only the document entity.
- Key line numbers use a textual search heuristic and can misattribute on
  duplicate keys, keys inside multiline strings, or commented-out keys that
  look real.

## Gate coverage

| Invariant | Gate |
| --- | --- |
| Exact entities/relationships/locations through `GraphBuilder` | `test_graph_builder_references.py::test_graph_builder_matches_fixture_contract` (parametrized over every fixture) |
| Unique entity IDs; collisions reported | `test_graph_integrity_gates.py::test_duplicate_declarations_never_produce_duplicate_entity_ids` |
| Mixed-language merge has no invalid/dangling endpoints | `test_graph_integrity_gates.py::test_mixed_language_merge_has_no_invalid_or_dangling_endpoints` |
| Output independent of scan order | `test_graph_integrity_gates.py::test_mixed_language_graph_is_independent_of_scan_order` |
| Malformed input is visible and deterministic | `tests/unit/parsers/test_parser_negative_fixtures.py` |
| Extension dispatch (`.tsx`, `.jsx`, Vue TS) | `tests/unit/parsers/test_parser_extension_dispatch.py` |
