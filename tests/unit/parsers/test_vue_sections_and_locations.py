"""Step 04 contract: robust Vue SFC section scanning and exact entity locations.

These tests pin two behaviors that the attribute-order-sensitive regex scanner
could not provide:

1. Every supported ``<template>``/``<script>``/``<style>`` attribute variant is
   recognized, and a malformed section is reported instead of silently dropped.
2. Every extracted entity carries the line range and source of its *actual*
   declaration rather than the enclosing script tag's offset.

Entity ID, kind, and qualified-name identity remain Step 05's scope, so these
tests address entities by name and assert locations, not IDs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import Entity, ParseResult
from knowcode.parsers.vue_parser import VueParser
from knowcode.utils.entity_identity import build_external_reference_id


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts" / "vue"


def _parse_source(tmp_path: Path, source: str, name: str = "Widget.vue") -> ParseResult:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return VueParser().parse_file(path)


def _entity(result: ParseResult, name: str) -> Entity:
    matches = [entity for entity in result.entities if entity.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} entity, got {matches}"
    return matches[0]


def _lines(result: ParseResult, name: str) -> tuple[int, int]:
    location = _entity(result, name).location
    return location.line_start, location.line_end


def _names(result: ParseResult) -> set[str]:
    return {entity.name for entity in result.entities}


# ---------------------------------------------------------------------------
# Section scanning: attributes, quoting, order, and case
# ---------------------------------------------------------------------------


def test_root_template_attributes_do_not_defeat_extraction(tmp_path: Path) -> None:
    """A root ``<template>`` carrying attributes must still be scanned."""
    result = _parse_source(
        tmp_path,
        """<template lang="html" data-view="contract">
  <button @click="increment">{{ count }}</button>
</template>
<script setup>
const count = ref(0)
function increment() { count.value++ }
</script>
""",
    )

    assert result.errors == []
    handler_targets = [
        relationship.target_id
        for relationship in result.relationships
        if relationship.kind.value == "calls"
    ]
    assert any("increment" in target for target in handler_targets), (
        "root template attributes suppressed template extraction"
    )


@pytest.mark.parametrize(
    "script_tag",
    [
        '<script setup lang="ts">',
        '<script lang="ts" setup>',
        "<script lang='ts' setup>",
        "<script setup lang='ts'>",
        '<script   setup   lang = "ts"  >',
        "<SCRIPT SETUP LANG='TS'>",
    ],
)
def test_script_setup_attribute_variants_are_recognized(
    tmp_path: Path, script_tag: str
) -> None:
    """``setup``/``lang`` in any order, quoting, spacing, or case must parse."""
    result = _parse_source(
        tmp_path,
        f"""<template>
  <div>{{{{ title }}}}</div>
</template>
{script_tag}
const title = ref('Contract')
</script>
""",
    )

    assert result.errors == []
    assert "title" in _names(result), f"{script_tag} defeated script extraction"

    component = _entity(result, "Widget")
    assert component.metadata["vue_api"] == "composition"
    assert component.metadata["script_lang"] == "ts"


@pytest.mark.parametrize(
    "style_tag",
    [
        "<style scoped lang='css'>",
        '<style lang="css" scoped>',
        "<STYLE SCOPED>",
    ],
)
def test_style_attribute_variants_are_recognized(
    tmp_path: Path, style_tag: str
) -> None:
    """``<style>`` attribute order, quoting, and case must not hide the block."""
    result = _parse_source(
        tmp_path,
        f"""<template>
  <div class="box" />
</template>
<script setup>
const accent = ref('red')
</script>
{style_tag}
.box {{ color: v-bind(accent); }}
</style>
""",
    )

    assert result.errors == []
    assert any(
        relationship.metadata.get("binding_type") == "css_variable"
        for relationship in result.relationships
    ), f"{style_tag} defeated style extraction"


def test_uppercase_template_tag_is_recognized(tmp_path: Path) -> None:
    """Vue tolerates uppercase section tags; the scanner must too."""
    result = _parse_source(
        tmp_path,
        """<TEMPLATE>
  <input v-model="count" />
</TEMPLATE>
<script setup>
const count = ref(0)
</script>
""",
    )

    assert result.errors == []
    assert any(
        "count" in relationship.target_id
        for relationship in result.relationships
        if relationship.kind.value == "references"
    ), "uppercase template tag defeated extraction"


def test_nested_template_with_attributes_is_bounded(tmp_path: Path) -> None:
    """A nested attribute-bearing ``<template>`` must not truncate the root."""
    result = _parse_source(
        tmp_path,
        """<template lang="html">
  <TableView>
    <template #header="{ column }">
      <span>{{ column }}</span>
    </template>
    <button @click="refresh">Refresh</button>
  </TableView>
</template>
<script setup>
function refresh() { return 1 }
</script>
""",
    )

    assert result.errors == []
    assert any(
        "refresh" in relationship.target_id
        for relationship in result.relationships
        if relationship.kind.value == "calls"
    ), "nested template truncated the root template content"


def test_script_setup_block_wins_over_plain_script(tmp_path: Path) -> None:
    """With both blocks present the ``setup`` block defines the exposed surface."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div>{{ title }}</div>
</template>
<script>
export default { name: 'Widget' }
</script>
<script setup>
const title = ref('Contract')
</script>
""",
    )

    assert result.errors == []
    assert "title" in _names(result)
    assert _entity(result, "Widget").metadata["vue_api"] == "composition"


def test_imports_are_collected_from_every_script_block(tmp_path: Path) -> None:
    """A plain block's imports must survive alongside a ``setup`` block."""
    result = _parse_source(
        tmp_path,
        """<template>
  <BaseCard><BaseIcon /></BaseCard>
</template>
<script>
import BaseCard from './BaseCard.vue'
export default { name: 'Widget' }
</script>
<script setup>
import BaseCard from './BaseCard.vue'
import BaseIcon from './BaseIcon.vue'
</script>
""",
    )

    imported = [
        relationship.target_id
        for relationship in result.relationships
        if relationship.kind.value == "imports"
    ]
    assert sorted(imported) == [
        build_external_reference_id("vue_component", "BaseCard"),
        build_external_reference_id("vue_component", "BaseIcon"),
    ], "an import repeated across blocks must be emitted once"


# ---------------------------------------------------------------------------
# Malformed sections must be visible, not silently dropped
# ---------------------------------------------------------------------------


def test_unclosed_script_section_is_reported(tmp_path: Path) -> None:
    """An unterminated ``<script>`` must surface an error, not vanish."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div>{{ title }}</div>
</template>
<script setup>
const title = ref('Contract')
""",
    )

    assert any("script" in error.lower() for error in result.errors), (
        f"unclosed script silently dropped; errors={result.errors}"
    )


def test_unclosed_template_section_is_reported(tmp_path: Path) -> None:
    """An unterminated ``<template>`` must surface an error, not vanish."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div>{{ title }}</div>
<script setup>
const title = ref('Contract')
</script>
""",
    )

    assert any("template" in error.lower() for error in result.errors), (
        f"unclosed template silently dropped; errors={result.errors}"
    )


def test_well_formed_component_reports_no_errors(tmp_path: Path) -> None:
    """The malformed-section check must not produce false positives."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div>{{ title }}</div>
</template>
<script setup>
const title = ref('Contract')
</script>
<style scoped>
div { color: red; }
</style>
""",
    )

    assert result.errors == []


# ---------------------------------------------------------------------------
# Exact locations against the committed Step 01 fixtures
# ---------------------------------------------------------------------------


def test_sfc_sections_fixture_locations_are_exact() -> None:
    """``sfc_sections.vue`` pins single-quoted ``lang`` plus exact declarations."""
    result = VueParser().parse_file(FIXTURE_ROOT / "sfc_sections.vue")

    assert result.errors == []
    assert _lines(result, "title") == (5, 5)
    assert _lines(result, "select") == (6, 8)


def test_composition_fixture_locations_are_exact() -> None:
    """Each Composition API declaration keeps its own line, not the tag's."""
    result = VueParser().parse_file(FIXTURE_ROOT / "composition_relationships.vue")

    assert result.errors == []
    assert _lines(result, "count") == (7, 7)
    assert _lines(result, "doubled") == (8, 8)
    assert _lines(result, "increment") == (9, 9)


def test_options_fixture_locations_are_exact() -> None:
    """Options API data and methods resolve to their declaration lines."""
    result = VueParser().parse_file(FIXTURE_ROOT / "options_relationships.vue")

    assert result.errors == []
    assert _lines(result, "count") == (8, 8)
    assert _lines(result, "increment") == (11, 13)


def test_entity_source_covers_the_declaration() -> None:
    """Snippets must contain the whole declaration, not a tag-line placeholder."""
    result = VueParser().parse_file(FIXTURE_ROOT / "sfc_sections.vue")

    select_source = _entity(result, "select").source_code or ""
    assert "function select()" in select_source
    assert "return title.value" in select_source

    title_source = _entity(result, "title").source_code or ""
    assert "const title = ref('Contract')" in title_source
    assert "function select" not in title_source


# ---------------------------------------------------------------------------
# Location correctness for multi-line and easily-confused declarations
# ---------------------------------------------------------------------------


def test_multiline_declarations_span_their_full_range(tmp_path: Path) -> None:
    """A multi-line declaration must report its real closing line."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div />
</template>
<script setup>
const config = reactive({
  retries: 3,
  timeout: 30,
})

const submit = async () => {
  await save(config)
}
</script>
""",
    )

    assert result.errors == []
    assert _lines(result, "config") == (5, 8)
    assert _lines(result, "submit") == (10, 12)


def test_options_api_computed_and_data_share_no_location(tmp_path: Path) -> None:
    """Duplicate names in different Options blocks keep distinct declarations."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div>{{ total }}</div>
</template>
<script>
export default {
  data() {
    return { count: 0 }
  },
  computed: {
    total() {
      return this.count * 2
    }
  }
}
</script>
""",
    )

    assert result.errors == []
    assert _lines(result, "count") == (7, 7)
    assert _lines(result, "total") == (10, 12)


def test_define_props_and_emits_use_their_declaration_lines(tmp_path: Path) -> None:
    """Props and emits must not collapse onto the script tag's line."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div />
</template>
<script setup>
const props = defineProps({
  title: String,
})

const emit = defineEmits(['close'])
</script>
""",
    )

    assert result.errors == []
    assert _lines(result, "props") == (5, 7)
    assert _lines(result, "title") == (6, 6)
    assert _lines(result, "close") == (9, 9)


def test_dollar_prefixed_reactive_declaration_is_classified(tmp_path: Path) -> None:
    """``$``-prefixed identifiers are legal JS and must not break detection."""
    result = _parse_source(
        tmp_path,
        """<template>
  <div />
</template>
<script setup>
const $el = ref(null)
</script>
""",
    )

    assert result.errors == []
    element = _entity(result, "$el")
    assert element.metadata["is_reactive"] == "true"
    assert element.location.line_start == 5
