"""Brace-aware object scanning must ignore braces that are not structure.

``find_balanced_block`` and ``top_level_keys`` decide which names become Vue
component entities. A brace inside a string or comment that is treated as
structure shifts the whole nesting count, which silently promotes nested names
to declarations or drops real ones.
"""

from __future__ import annotations

import pytest

from knowcode.parsers.vue_object_scan import find_balanced_block, top_level_keys


# --- find_balanced_block --------------------------------------------------


def test_returns_body_between_matching_braces() -> None:
    text = "methods: { save() { return 1 } } trailing"

    assert find_balanced_block(text, text.index("{")) == " save() { return 1 } "


@pytest.mark.parametrize(
    "text, index",
    [
        ("no brace here", 0),
        ("{ unterminated", 0),
        ("{ nested { unterminated }", 0),
        ("", 0),
        ("{}", 5),
    ],
)
def test_returns_none_when_no_balanced_block_starts_there(text: str, index: int) -> None:
    assert find_balanced_block(text, index) is None


def test_braces_inside_strings_do_not_shift_nesting() -> None:
    text = """{ label: "a } b", other: 'c { d', tpl: `e } f` }"""

    body = find_balanced_block(text, 0)

    assert body is not None
    assert top_level_keys(body) == ["label", "other", "tpl"]


def test_escaped_quote_does_not_end_a_string() -> None:
    text = r"""{ label: "a \" } still string", after: 1 }"""

    body = find_balanced_block(text, 0)

    assert body is not None
    assert top_level_keys(body) == ["label", "after"]


def test_unterminated_string_does_not_hang_or_overrun() -> None:
    assert find_balanced_block('{ label: "never closed', 0) is None


def test_braces_inside_comments_do_not_shift_nesting() -> None:
    text = """{
  // a stray } brace
  save() { return 1 },
  /* another } here
     spanning lines */
  reset() { return 2 }
}"""

    body = find_balanced_block(text, 0)

    assert body is not None
    assert top_level_keys(body) == ["save", "reset"]


def test_unterminated_block_comment_runs_to_end() -> None:
    assert find_balanced_block("{ save() {} /* never closed", 0) is None


# --- top_level_keys -------------------------------------------------------


def test_returns_shorthand_methods_and_properties_in_source_order() -> None:
    body = " zulu() { return 1 }, alpha: 2, mike: { nested: 3 } "

    assert top_level_keys(body) == ["zulu", "alpha", "mike"]


def test_ignores_names_nested_in_objects_arrays_and_argument_lists() -> None:
    body = """
      save(payload, options) {
        if (payload.ok) { return { inner: 1 } }
        for (const row of rows) { row.touch() }
      },
      config: { type: String, default: 'x' },
      list: [{ hidden: 1 }]
    """

    assert top_level_keys(body) == ["save", "config", "list"]


def test_ignores_keys_appearing_only_inside_comments() -> None:
    body = "// ghost: 1\nreal: 2\n/* phantom: 3 */"

    assert top_level_keys(body) == ["real"]


def test_quoted_keys_are_not_reported_as_identifiers() -> None:
    """Quoted keys are legal Vue but are not identifiers.

    They are skipped as string content rather than mis-parsed; recovering them
    is tracked as a separate parser-coverage gap.
    """
    body = "'quoted': 1, plain: 2"

    assert top_level_keys(body) == ["plain"]


def test_empty_body_has_no_keys() -> None:
    assert top_level_keys("") == []
    assert top_level_keys("   \n  ") == []
