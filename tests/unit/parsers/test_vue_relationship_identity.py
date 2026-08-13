"""Step 05 contract: Vue template/script/style edges resolve to real entities.

Every internal-looking relationship endpoint a Vue component emits must be an
entity that the same parse produced. Names the component does not declare become
explicit unresolved references carrying diagnostic metadata, and symbols that
live outside the component stay explicit external references. These tests assert
endpoint identity and kind, never target substrings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import EntityKind, ParseResult, RelationshipKind
from knowcode.parsers.vue_parser import VueParser
from knowcode.utils.entity_identity import (
    EndpointKind,
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    classify_endpoint_id,
)
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    assert_relationship_endpoints_classified,
    load_parser_fixture_contract,
)

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts" / "vue"

# Legacy pseudo-namespaces this step removes. They looked internal but never
# matched an extracted entity.
FABRICATED_ID_MARKERS = (
    "::method::",
    "::data::",
    "::ref::",
    "::const::",
    "::let::",
    "::var::",
    "::function::",
    "::computed::",
    "::prop::",
    "::emit::",
)


@pytest.fixture(name="parser")
def _parser() -> VueParser:
    return VueParser()


def write_component(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.vue"
    path.write_text(source, encoding="utf-8")
    return path


def entity_by_qualified_name(result: ParseResult, qualified_name: str):
    matches = [e for e in result.entities if e.qualified_name == qualified_name]
    assert len(matches) == 1, (
        f"expected exactly one {qualified_name!r} entity, got "
        f"{[e.qualified_name for e in result.entities]}"
    )
    return matches[0]


def edges(result: ParseResult, kind: RelationshipKind, binding_type: str) -> list:
    return [
        relationship
        for relationship in result.relationships
        if relationship.kind is kind
        and relationship.metadata.get("binding_type") == binding_type
    ]


def only_edge(result: ParseResult, kind: RelationshipKind, binding_type: str):
    matches = edges(result, kind, binding_type)
    assert len(matches) == 1, (
        f"expected exactly one {binding_type} {kind.value} edge, got "
        f"{[(r.kind.value, r.target_id, r.metadata) for r in result.relationships]}"
    )
    return matches[0]


# --- committed fixture contracts -----------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    ["composition_relationships.vue", "options_relationships.vue", "sfc_sections.vue"],
)
def test_committed_vue_fixture_graph_is_exact(parser: VueParser, fixture_name: str) -> None:
    contract = load_parser_fixture_contract(FIXTURE_ROOT / fixture_name)

    assert_exact_parse_result(parser.parse_file(contract.source_path), contract)


# --- Composition API resolution ------------------------------------------


COMPOSITION_COMPONENT = """<template>
  <button @click="increment">{{ doubled }}</button>
  <input v-model="count" />
  <p>{{ label }}</p>
</template>
<script setup>
import { computed, ref } from 'vue'
const props = defineProps({ label: String })
const count = ref(0)
const doubled = computed(() => count.value * 2)
function increment() { count.value++ }
</script>
<style scoped>
.counter { width: v-bind(doubled); color: v-bind(label); }
</style>
"""


def test_composition_event_handler_resolves_to_the_extracted_function(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Counter", COMPOSITION_COMPONENT)

    result = parser.parse_file(path)

    handler = entity_by_qualified_name(result, "Counter.increment")
    assert handler.kind is EntityKind.FUNCTION
    edge = only_edge(result, RelationshipKind.CALLS, "event")
    assert edge.target_id == handler.id
    assert edge.source_id == build_internal_entity_id(path, "Counter")


def test_composition_v_model_resolves_to_the_extracted_ref(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Counter", COMPOSITION_COMPONENT)

    result = parser.parse_file(path)

    count = entity_by_qualified_name(result, "Counter.count")
    assert count.kind is EntityKind.VARIABLE
    assert count.metadata["is_reactive"] == "true"
    assert only_edge(result, RelationshipKind.REFERENCES, "model").target_id == count.id


def test_composition_css_bindings_resolve_to_computed_and_prop_entities(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Counter", COMPOSITION_COMPONENT)

    result = parser.parse_file(path)

    doubled = entity_by_qualified_name(result, "Counter.doubled")
    label = entity_by_qualified_name(result, "Counter.label")
    assert label.metadata["declaration_type"] == "prop"
    css_targets = {
        edge.target_id for edge in edges(result, RelationshipKind.REFERENCES, "css_variable")
    }
    assert css_targets == {doubled.id, label.id}


def test_composition_entity_ids_are_canonical_and_contained(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Counter", COMPOSITION_COMPONENT)

    result = parser.parse_file(path)

    component_id = build_internal_entity_id(path, "Counter")
    for name in ("count", "doubled", "increment", "label"):
        assert entity_by_qualified_name(result, f"Counter.{name}").id == (
            build_internal_entity_id(path, f"Counter.{name}")
        )
    contained = {
        edge.target_id
        for edge in result.relationships
        if edge.kind is RelationshipKind.CONTAINS and edge.source_id == component_id
    }
    assert contained == {
        entity.id for entity in result.entities if entity.id != component_id
    }


# --- Options API resolution ----------------------------------------------


OPTIONS_COMPONENT = """<template>
  <button @click="increment">{{ doubled }}</button>
  <input v-model="count" />
</template>
<script>
export default {
  data() {
    return { count: 0 }
  },
  computed: {
    doubled() {
      return this.count * 2
    }
  },
  methods: {
    increment() {
      this.count++
    }
  }
}
</script>
<style scoped>
.counter { width: v-bind(doubled); }
</style>
"""


def test_options_event_handler_resolves_to_a_method_entity(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Widget", OPTIONS_COMPONENT)

    result = parser.parse_file(path)

    increment = entity_by_qualified_name(result, "Widget.increment")
    assert increment.kind is EntityKind.METHOD
    assert only_edge(result, RelationshipKind.CALLS, "event").target_id == increment.id


def test_options_model_and_css_resolve_to_data_and_computed_entities(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Widget", OPTIONS_COMPONENT)

    result = parser.parse_file(path)

    count = entity_by_qualified_name(result, "Widget.count")
    doubled = entity_by_qualified_name(result, "Widget.doubled")
    assert count.metadata["declaration_type"] == "data"
    assert doubled.metadata["declaration_type"] == "computed"
    assert only_edge(result, RelationshipKind.REFERENCES, "model").target_id == count.id
    assert (
        only_edge(result, RelationshipKind.REFERENCES, "css_variable").target_id
        == doubled.id
    )


def test_options_data_and_computed_entities_are_contained(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Widget", OPTIONS_COMPONENT)

    result = parser.parse_file(path)

    component_id = build_internal_entity_id(path, "Widget")
    contained = {
        edge.target_id
        for edge in result.relationships
        if edge.kind is RelationshipKind.CONTAINS and edge.source_id == component_id
    }
    assert contained == {
        entity.id for entity in result.entities if entity.id != component_id
    }


# --- unresolved references ------------------------------------------------


UNDECLARED_COMPONENT = """<template>
  <button @click="missingHandler">go</button>
  <input v-model="missingModel" />
</template>
<script setup>
const declared = ref(0)
</script>
<style scoped>
.a { color: v-bind(missingColor); }
</style>
"""


@pytest.mark.parametrize(
    "kind, binding_type, symbol",
    [
        (RelationshipKind.CALLS, "event", "missingHandler"),
        (RelationshipKind.REFERENCES, "model", "missingModel"),
        (RelationshipKind.REFERENCES, "css_variable", "missingColor"),
    ],
)
def test_undeclared_template_names_become_scoped_unresolved_references(
    parser: VueParser,
    tmp_path: Path,
    kind: RelationshipKind,
    binding_type: str,
    symbol: str,
) -> None:
    path = write_component(tmp_path, "Undeclared", UNDECLARED_COMPONENT)

    result = parser.parse_file(path)

    edge = only_edge(result, kind, binding_type)
    assert edge.target_id == build_unresolved_reference_id(
        "vue", path, "Undeclared", symbol
    )
    assert classify_endpoint_id(edge.target_id) is EndpointKind.UNRESOLVED
    assert edge.metadata["resolution"] == "unresolved"
    assert edge.metadata["symbol"] == symbol


def test_no_fabricated_internal_namespace_is_emitted(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(tmp_path, "Undeclared", UNDECLARED_COMPONENT)

    result = parser.parse_file(path)

    endpoints = [entity.id for entity in result.entities] + [
        endpoint
        for relationship in result.relationships
        for endpoint in (relationship.source_id, relationship.target_id)
    ]
    for endpoint in endpoints:
        for marker in FABRICATED_ID_MARKERS:
            assert marker not in endpoint, f"fabricated namespace in {endpoint!r}"


def test_every_endpoint_is_classified_for_a_full_component(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(
        tmp_path,
        "FullStack",
        """<template>
  <MyButton @click="submit" />
  <input v-model="draft" />
  <span>{{ unknownThing }}</span>
</template>
<script setup>
import MyButton from './MyButton.vue'
import { useRouter } from 'vue-router'
const router = useRouter()
const draft = ref('')
const emit = defineEmits(['saved'])
function submit() { emit('saved', draft.value) }
</script>
<style scoped>
.x { color: v-bind(draft); }
</style>
""",
    )

    result = parser.parse_file(path)

    assert_relationship_endpoints_classified(result)


# --- external references --------------------------------------------------


def test_package_imports_are_external_module_references(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(
        tmp_path,
        "Imports",
        """<template><div /></template>
<script setup>
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
</script>
""",
    )

    result = parser.parse_file(path)

    imports = {
        edge.target_id
        for edge in result.relationships
        if edge.kind is RelationshipKind.IMPORTS
    }
    assert imports == {
        build_external_reference_id("npm", "vue"),
        build_external_reference_id("npm", "pinia"),
    }


def test_path_imports_and_template_usage_are_external_component_references(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(
        tmp_path,
        "Tree",
        """<template>
  <MyButton />
  <user-profile />
</template>
<script setup>
import MyButton from './MyButton.vue'
import { UserProfile } from '@/components'
</script>
""",
    )

    result = parser.parse_file(path)

    imports = {
        edge.target_id
        for edge in result.relationships
        if edge.kind is RelationshipKind.IMPORTS
    }
    usages = {
        edge.target_id
        for edge in result.relationships
        if edge.kind is RelationshipKind.REFERENCES
        and edge.metadata.get("usage_type") == "template"
    }
    assert imports == {
        build_external_reference_id("vue_component", "MyButton"),
        build_external_reference_id("vue_component", "UserProfile"),
    }
    assert usages == {
        build_external_reference_id("vue_component", "MyButton"),
        build_external_reference_id("vue_component", "UserProfile"),
    }


def test_composable_calls_are_external_unless_declared_locally(
    parser: VueParser, tmp_path: Path
) -> None:
    external = write_component(
        tmp_path,
        "External",
        """<template><div /></template>
<script setup>
import { useRouter } from 'vue-router'
const router = useRouter()
</script>
""",
    )
    local = write_component(
        tmp_path,
        "Local",
        """<template><div /></template>
<script setup>
function useLocalThing() { return 1 }
const value = useLocalThing()
</script>
""",
    )

    external_result = parser.parse_file(external)
    local_result = parser.parse_file(local)

    external_calls = {
        edge.target_id
        for edge in external_result.relationships
        if edge.kind is RelationshipKind.CALLS
    }
    assert external_calls == {build_external_reference_id("composable", "useRouter")}

    local_calls = {
        edge.target_id
        for edge in local_result.relationships
        if edge.kind is RelationshipKind.CALLS
    }
    assert local_calls == {build_internal_entity_id(local, "Local.useLocalThing")}


# --- determinism and collisions -------------------------------------------


def test_repeated_parses_produce_identical_graphs(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(
        tmp_path,
        "Deterministic",
        """<template><div @click="go">{{ alpha }}</div></template>
<script setup>
const props = defineProps({ alpha: String, beta: Number, gamma: Boolean })
function go() {}
</script>
""",
    )

    def snapshot() -> tuple:
        result = VueParser().parse_file(path)
        return (
            tuple(
                (e.id, e.kind, e.qualified_name, e.location.line_start)
                for e in result.entities
            ),
            tuple(
                (r.source_id, r.target_id, r.kind) for r in result.relationships
            ),
        )

    assert snapshot() == snapshot()
    # Self-comparison cannot catch set-ordered emission, because ``str`` hashing
    # is only randomized between processes. Pin the declared source order too.
    props = [
        entity.qualified_name
        for entity in parser.parse_file(path).entities
        if entity.metadata.get("declaration_type") == "prop"
    ]
    assert props == [
        "Deterministic.alpha",
        "Deterministic.beta",
        "Deterministic.gamma",
    ]


def test_colliding_binding_names_do_not_duplicate_entity_ids(
    parser: VueParser, tmp_path: Path
) -> None:
    path = write_component(
        tmp_path,
        "Collision",
        """<template><div>{{ count }}</div></template>
<script>
export default {
  data() {
    return { count: 0 }
  },
  computed: {
    count() {
      return 1
    }
  }
}
</script>
""",
    )

    result = parser.parse_file(path)

    ids = [entity.id for entity in result.entities]
    assert len(ids) == len(set(ids))
    assert result.errors, "a duplicate template binding name must be reported"
