"""Branch-level contract for the bounded Vue SFC section scanner.

``test_vue_sections_and_locations`` covers the scanner through ``VueParser``.
These tests drive ``scan_sfc_sections`` directly so every malformed-input and
attribute-form branch has an explicit, named expectation.
"""

from __future__ import annotations

import pytest

from knowcode.parsers.vue_sections import scan_sfc_sections


def _tags(source: str) -> list[str]:
    return [section.tag for section in scan_sfc_sections(source).sections]


# ---------------------------------------------------------------------------
# Attribute forms
# ---------------------------------------------------------------------------


def test_unquoted_attribute_value_is_read() -> None:
    scan = scan_sfc_sections("<script lang=ts setup>const a = 1</script>")

    section = scan.sections[0]
    assert scan.errors == ()
    assert section.lang == "ts"
    assert section.is_setup is True
    assert section.content == "const a = 1"


def test_attribute_value_may_contain_a_greater_than_sign() -> None:
    """A ``>`` inside a quoted value must not terminate the tag."""
    scan = scan_sfc_sections('<template data-expr="a>b"><p /></template>')

    section = scan.sections[0]
    assert scan.errors == ()
    assert section.attributes["data-expr"] == "a>b"
    assert section.content == "<p />"


def test_valueless_and_repeated_attributes() -> None:
    scan = scan_sfc_sections("<style scoped lang='scss' lang='css'>a{}</style>")

    section = scan.sections[0]
    assert section.attributes["scoped"] == ""
    assert section.lang == "scss", "first value must win"


def test_stray_solidus_inside_tag_is_tolerated() -> None:
    scan = scan_sfc_sections("<script / setup>const a = 1</script>")

    assert scan.sections[0].is_setup is True


def test_self_closing_block_yields_empty_content() -> None:
    scan = scan_sfc_sections("<template />\n<script setup>const a = 1</script>")

    template = scan.sections[0]
    assert template.tag == "template"
    assert template.content == ""
    assert _tags("<template />\n<script setup>const a = 1</script>") == [
        "template",
        "script",
    ]


# ---------------------------------------------------------------------------
# Tag matching boundaries
# ---------------------------------------------------------------------------


def test_tag_name_prefix_is_not_a_section() -> None:
    assert _tags("<templates><p /></templates>") == []


def test_close_tag_tolerates_trailing_whitespace() -> None:
    scan = scan_sfc_sections("<template><p /></template >")

    assert scan.errors == ()
    assert scan.sections[0].content == "<p />"


def test_close_tag_prefix_does_not_end_the_section() -> None:
    """``</template2>`` must not be mistaken for the real close tag."""
    scan = scan_sfc_sections("<template><p></template2></p></template>")

    assert scan.errors == ()
    assert scan.sections[0].content == "<p></template2></p>"


def test_nested_templates_are_depth_counted() -> None:
    scan = scan_sfc_sections("<template><template #a><p /></template><b /></template>")

    assert scan.errors == ()
    assert scan.sections[0].content == "<template #a><p /></template><b />"


def test_sections_are_scanned_in_document_order() -> None:
    assert _tags("<template><p /></template><script>1</script><style>a{}</style>") == [
        "template",
        "script",
        "style",
    ]


# ---------------------------------------------------------------------------
# Malformed input must be visible
# ---------------------------------------------------------------------------


def test_unclosed_style_is_reported() -> None:
    scan = scan_sfc_sections("<template><p /></template>\n<style>.a { color: red; }")

    assert _tags("<template><p /></template>\n<style>.a { color: red; }") == [
        "template"
    ]
    assert scan.errors == ("Unclosed <style> section starting on line 2",)


def test_unterminated_attribute_quote_is_reported() -> None:
    scan = scan_sfc_sections('<script lang="ts>const a = 1</script>')

    assert scan.sections == ()
    assert scan.errors == ("Malformed <script> tag on line 1",)


@pytest.mark.parametrize(
    "source",
    [
        "<script",
        "<script ",
        "<script lang=",
        '<script lang="ts"',
        "<script =value>",
    ],
)
def test_truncated_or_nameless_tags_are_reported(source: str) -> None:
    scan = scan_sfc_sections(source)

    assert scan.sections == ()
    assert scan.errors == ("Malformed <script> tag on line 1",)


def test_a_well_formed_document_reports_no_errors() -> None:
    scan = scan_sfc_sections(
        "<template>\n  <p />\n</template>\n<script setup>\nconst a = 1\n</script>\n"
    )

    assert scan.errors == ()


# ---------------------------------------------------------------------------
# Coordinates used for rebasing script locations
# ---------------------------------------------------------------------------


def test_section_coordinates_are_one_based_and_exact() -> None:
    source = (
        "<template>\n"  # line 1
        "  <p />\n"  # line 2
        "</template>\n"  # line 3
        "<script\n"  # line 4
        "  setup\n"  # line 5
        ">\n"  # line 6
        "const a = 1\n"  # line 7
        "</script>\n"  # line 8
    )
    scan = scan_sfc_sections(source)

    template = scan.first("template")
    assert template is not None
    assert (
        template.tag_line_start,
        template.content_line_start,
        template.line_end,
    ) == (
        1,
        1,
        3,
    )

    script = scan.script()
    assert script is not None
    assert script.is_setup is True
    # The tag spans lines 4-6, so content begins on the line holding '>'.
    assert (script.tag_line_start, script.content_line_start, script.line_end) == (
        4,
        6,
        8,
    )


def test_script_setup_block_is_preferred_over_a_plain_script() -> None:
    scan = scan_sfc_sections(
        "<script>export default {}</script><script setup>const a = 1</script>"
    )

    script = scan.script()
    assert script is not None
    assert script.is_setup is True


def test_first_returns_none_for_an_absent_tag() -> None:
    scan = scan_sfc_sections("<template><p /></template>")

    assert scan.first("style") is None
    assert scan.script() is None
