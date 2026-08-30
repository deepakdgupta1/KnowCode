"""Tests for encoding repository-anchored ids relative to their root."""

import pytest

from knowcode.utils.entity_identity import (
    absolutize_id,
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    relativize_id,
)

ROOT = "/repo/root"


def _roundtrip(value: str, root: str = ROOT) -> str:
    stored = relativize_id(value, root)
    assert absolutize_id(stored, root) == value
    return stored


def test_internal_id_loses_the_root_and_round_trips() -> None:
    value = build_internal_entity_id(f"{ROOT}/src/mod.py", "Klass.method")

    stored = _roundtrip(value)

    assert stored == "src/mod.py::Klass.method"
    assert ROOT not in stored


def test_bare_file_path_loses_the_root_and_round_trips() -> None:
    stored = _roundtrip(f"{ROOT}/src/mod.py")

    assert stored == "src/mod.py"


def test_external_id_carries_no_path_and_is_untouched() -> None:
    value = build_external_reference_id("requests", "Session.get")

    assert _roundtrip(value) == value


def test_unresolved_id_loses_the_root_inside_its_encoded_file() -> None:
    value = build_unresolved_reference_id(
        "python", f"{ROOT}/src/mod.py", "Klass.method", "helper"
    )

    stored = _roundtrip(value)

    assert stored == "unresolved::python::src/mod.py::Klass.method::helper"
    assert ROOT not in stored


def test_a_path_outside_the_root_stays_absolute_and_round_trips() -> None:
    value = build_internal_entity_id("/elsewhere/vendor.py", "helper")

    assert _roundtrip(value) == value


def test_a_root_needing_percent_encoding_round_trips() -> None:
    root = "/repo/my root"
    value = build_unresolved_reference_id(
        "python", f"{root}/src/mod.py", "scope", "sym"
    )

    stored = _roundtrip(value, root)

    assert "my%20root" not in stored


@pytest.mark.parametrize("root", [ROOT, f"{ROOT}/"])
def test_a_trailing_separator_on_the_root_does_not_change_the_encoding(
    root: str,
) -> None:
    value = build_internal_entity_id(f"{ROOT}/src/mod.py", "fn")

    assert relativize_id(value, root) == "src/mod.py::fn"


def test_an_already_relative_id_is_not_stripped_twice() -> None:
    value = build_internal_entity_id(f"{ROOT}/src/mod.py", "fn")

    once = relativize_id(value, ROOT)

    assert relativize_id(once, ROOT) == once


def test_the_root_itself_is_not_a_valid_id_prefix_match() -> None:
    value = build_internal_entity_id("/repo/rootsibling/mod.py", "fn")

    assert _roundtrip(value) == value
