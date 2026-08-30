"""Hostile and malformed Vue input must degrade visibly, never crash or fabricate.

Step 05 made every Vue relationship endpoint canonical. These tests cover the
inputs that canonicalization made dangerous: a file whose name yields no
component name at all, a malformed import specifier, and declaration extractors
whose over-matching would put names the component never declared into the symbol
table, where a template reference would then falsely resolve to them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import ParseResult, RelationshipKind
from knowcode.parsers.vue_parser import VueParser
from knowcode.utils.entity_identity import (
    EndpointKind,
    build_external_reference_id,
    classify_endpoint_id,
)


@pytest.fixture(name="parser")
def _parser() -> VueParser:
    return VueParser()


def write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def names(result: ParseResult) -> set[str]:
    return {entity.name for entity in result.entities}


def edge_targets(result: ParseResult, kind: RelationshipKind) -> list[str]:
    return [
        relationship.target_id
        for relationship in result.relationships
        if relationship.kind is kind
    ]


# --- malformed input must not raise --------------------------------------


@pytest.mark.parametrize("filename", ["_.vue", "-.vue", "__.vue", "---.vue"])
def test_file_name_yielding_no_component_name_is_parsed(
    parser: VueParser, tmp_path: Path, filename: str
) -> None:
    """``pages/_.vue`` is a Nuxt catch-all route, not a malformed file.

    Deriving a PascalCase component name from such a stem leaves nothing, and an
    empty qualified name is rejected by the identity builder. The parser must
    still return a result rather than aborting the whole index build.
    """
    path = write(tmp_path, filename, "<template><div /></template>\n")

    result = parser.parse_file(path)

    # The file entity plus the component it holds (BL-10).
    assert len(result.entities) == 2
    component = result.entities[0]
    assert component.qualified_name
    assert classify_endpoint_id(component.id) is EndpointKind.INTERNAL


def test_blank_import_specifier_is_reported_not_raised(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "Blank.vue",
        """<template><div /></template>
<script setup>
import x from '   '
const value = 1
</script>
""",
    )

    result = parser.parse_file(path)

    assert edge_targets(result, RelationshipKind.IMPORTS) == []
    assert any("import" in error.lower() for error in result.errors)
    # The rest of the block must still be extracted.
    assert "value" in names(result)


# --- extractors must not fabricate template bindings ----------------------


def test_control_flow_in_a_method_body_is_not_a_method(
    parser: VueParser, tmp_path: Path
) -> None:
    """``if (...) {`` looks exactly like a method definition to a regex.

    A fabricated ``if`` binding would make ``@click="if"`` resolve to a real
    entity, which is worse than an unresolved reference: the graph would claim a
    handler that does not exist.
    """
    path = write(
        tmp_path,
        "Guarded.vue",
        """<template><button @click="if">x</button></template>
<script>
export default {
  methods: {
    save() {
      if (this.ok) { return 1 }
      for (const item of this.list) { item.touch() }
      while (this.busy) { this.wait() }
      switch (this.mode) { default: break }
    }
  }
}
</script>
""",
    )

    result = parser.parse_file(path)

    assert names(result) == {"Guarded", "save"}
    handler = edge_targets(result, RelationshipKind.CALLS)
    assert len(handler) == 1
    assert classify_endpoint_id(handler[0]) is EndpointKind.UNRESOLVED


def test_nested_define_props_option_keys_are_not_props(
    parser: VueParser, tmp_path: Path
) -> None:
    """Only the outer keys of a runtime ``defineProps`` object are props."""
    path = write(
        tmp_path,
        "Nested.vue",
        """<template><div @click="type" /></template>
<script setup>
const props = defineProps({
  label: { type: String, default: 'x', required: true },
  count: Number
})
</script>
""",
    )

    result = parser.parse_file(path)

    declared = {
        entity.name
        for entity in result.entities
        if entity.metadata.get("declaration_type") == "prop"
    }
    assert declared == {"label", "count"}
    handler = edge_targets(result, RelationshipKind.CALLS)
    assert len(handler) == 1
    assert classify_endpoint_id(handler[0]) is EndpointKind.UNRESOLVED


def test_object_literal_keys_in_a_computed_body_are_not_computed_properties(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "Shape.vue",
        """<template><div /></template>
<script>
export default {
  computed: {
    box() {
      return { width: 1, height: 2 }
    }
  }
}
</script>
""",
    )

    result = parser.parse_file(path)

    assert names(result) == {"Shape", "box"}


def test_reactive_props_destructure_is_not_a_duplicate_declaration(
    parser: VueParser, tmp_path: Path
) -> None:
    """``const { label } = defineProps(...)`` is Vue 3.5 reactive props destructure.

    The destructured name and the prop are one binding, so the component is
    correct and must not be reported as declaring ``label`` twice.
    """
    path = write(
        tmp_path,
        "Destructured.vue",
        """<template><div>{{ label }}</div></template>
<script setup>
const { label, count } = defineProps({ label: String, count: Number })
</script>
""",
    )

    result = parser.parse_file(path)

    assert result.errors == []
    assert names(result) == {"Destructured", "label", "count"}
    assert {
        entity.metadata["declaration_type"]
        for entity in result.entities
        if entity.name in {"label", "count"}
    } == {"prop"}


# --- silent loss must become a visible diagnostic -------------------------


def test_skipped_companion_script_block_is_reported(
    parser: VueParser, tmp_path: Path
) -> None:
    """Vue merges a plain ``<script>``'s options into a ``<script setup>`` component.

    Only the ``setup`` block is indexed today, so the Options API declarations of
    the companion block are missing and its template names resolve to nothing.
    That limitation must be visible rather than surfacing as a silent unresolved
    reference.
    """
    path = write(
        tmp_path,
        "Dual.vue",
        """<template><button @click="legacyMethod">x</button></template>
<script>
export default { methods: { legacyMethod() { return 1 } } }
</script>
<script setup>
const setupRef = ref(0)
</script>
""",
    )

    result = parser.parse_file(path)

    assert any("script" in error.lower() for error in result.errors), (
        "skipping a companion script block must be reported"
    )


# --- import scanning ------------------------------------------------------


def test_commented_and_quoted_imports_do_not_create_component_edges(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "Ghosts.vue",
        """<template><div /></template>
<script setup>
// import Ghost from './Ghost.vue'
import Real from './Real.vue'
const doc = 'run import Phantom from "./Phantom.vue" first'
</script>
""",
    )

    result = parser.parse_file(path)

    assert edge_targets(result, RelationshipKind.IMPORTS) == [
        build_external_reference_id("vue_component", "Real")
    ]


def test_type_only_imports_are_not_component_references(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "Typed.vue",
        """<template><div /></template>
<script setup lang="ts">
import type { UserRecord } from './types'
import Card from './Card.vue'
</script>
""",
    )

    result = parser.parse_file(path)

    assert edge_targets(result, RelationshipKind.IMPORTS) == [
        build_external_reference_id("vue_component", "Card")
    ]


def test_repeated_composable_calls_emit_one_edge(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write(
        tmp_path,
        "Repeat.vue",
        """<template><div /></template>
<script setup>
import { useStore } from 'vuex'
const a = useStore()
const b = useStore()
</script>
""",
    )

    result = parser.parse_file(path)

    assert edge_targets(result, RelationshipKind.CALLS) == [
        build_external_reference_id("composable", "useStore")
    ]


def test_no_relationship_is_emitted_twice(parser: VueParser, tmp_path: Path) -> None:
    """Repeating a binding in the template or style is one relationship.

    The identity of a Vue edge is its source, target, kind, and binding type. A
    name bound by both ``v-model`` and CSS ``v-bind()`` is deliberately two
    edges, because dropping either would lose a distinct fact about the
    component; the same name bound twice the same way is one.
    """
    path = write(
        tmp_path,
        "Dense.vue",
        """<template>
  <MyCard @click="save" />
  <MyCard @click="save" />
  <input v-model="draft" />
  <input v-model="draft" />
</template>
<script setup>
import MyCard from './MyCard.vue'
import { useStore } from 'vuex'
const store = useStore()
const draft = ref('')
function save() { useStore() }
</script>
<style scoped>
.a { color: v-bind(draft); }
.b { background: v-bind(draft); }
</style>
""",
    )

    result = parser.parse_file(path)

    keys = [
        (
            r.source_id,
            r.target_id,
            r.kind,
            r.metadata.get("binding_type") or r.metadata.get("usage_type"),
        )
        for r in result.relationships
    ]
    assert len(keys) == len(set(keys)), f"duplicate relationships: {keys}"

    # The two distinct facts about `draft` are both retained.
    draft_bindings = {
        r.metadata.get("binding_type")
        for r in result.relationships
        if r.target_id.endswith("::Dense.Dense.draft")
        and r.kind is RelationshipKind.REFERENCES
    }
    assert draft_bindings == {"model", "css_variable"}
