"""Tests for live source hydration from disk."""

from pathlib import Path

from knowcode.data_models import Entity, EntityKind, Location
from knowcode.analysis.live_source_loader import LiveSourceLoader


def test_live_hydration_modified_file(tmp_path: Path) -> None:
    file_path = tmp_path / "test.py"
    file_path.write_text("line 1\nline 2\nline 3\nline 4\n")

    loc = Location(file_path="test.py", line_start=2, line_end=3)
    entity = Entity(
        id="e1",
        kind=EntityKind.FUNCTION,
        name="foo",
        qualified_name="foo",
        location=loc,
        source_code="old line 2\nold line 3\n",
    )

    loader = LiveSourceLoader(tmp_path)
    source = loader.load_source(entity)
    assert source == "line 2\nline 3\n"


def test_live_hydration_deleted_file(tmp_path: Path) -> None:
    loc = Location(file_path="missing.py", line_start=1, line_end=2)
    entity = Entity(
        id="e2",
        kind=EntityKind.FUNCTION,
        name="bar",
        qualified_name="bar",
        location=loc,
        source_code="old code",
    )

    loader = LiveSourceLoader(tmp_path)
    source = loader.load_source(entity)
    assert source is None


def test_live_hydration_out_of_bounds(tmp_path: Path) -> None:
    file_path = tmp_path / "short.py"
    file_path.write_text("line 1\n")

    loc = Location(file_path="short.py", line_start=5, line_end=10)
    entity = Entity(
        id="e3",
        kind=EntityKind.FUNCTION,
        name="baz",
        qualified_name="baz",
        location=loc,
        source_code="old code",
    )

    loader = LiveSourceLoader(tmp_path)
    source = loader.load_source(entity)
    assert source is None


# ---------------------------------------------------------------------------
# Verified loads (storage plan D3)
# ---------------------------------------------------------------------------


def _indexed_alpha(tmp_path: Path) -> Entity:
    """Build an entity whose digest matches a two-line function on disk."""
    file_path = tmp_path / "mod.py"
    file_path.write_text("import os\n\n\ndef alpha():\n    return 1\n")
    entity = Entity(
        id="mod.py::alpha",
        kind=EntityKind.FUNCTION,
        name="alpha",
        qualified_name="alpha",
        location=Location(file_path="mod.py", line_start=4, line_end=5),
        source_code="def alpha():\n    return 1\n",
    )
    from knowcode.utils.entity_identity import compute_entity_content_hash

    entity.metadata["content_hash"] = compute_entity_content_hash(entity)
    return entity


def test_verified_source_resolves_a_clean_file(tmp_path: Path) -> None:
    loader = LiveSourceLoader(tmp_path)

    assert loader.load_verified_source(_indexed_alpha(tmp_path)) == (
        "def alpha():\n    return 1\n"
    )


def test_verified_source_fails_closed_on_an_edited_body(tmp_path: Path) -> None:
    entity = _indexed_alpha(tmp_path)
    (tmp_path / "mod.py").write_text("import os\n\n\ndef alpha():\n    return 42\n")

    assert LiveSourceLoader(tmp_path).load_verified_source(entity) is None


def test_verified_source_fails_closed_on_a_shifted_span(tmp_path: Path) -> None:
    """A line inserted above the entity moves it off its recorded lines; the
    re-read span no longer hashes to the indexed digest."""
    entity = _indexed_alpha(tmp_path)
    (tmp_path / "mod.py").write_text(
        "import os\n\n\n# a new line above\n\ndef alpha():\n    return 1\n"
    )

    assert LiveSourceLoader(tmp_path).load_verified_source(entity) is None


def test_verified_source_survives_an_unrelated_edit(tmp_path: Path) -> None:
    """An edit outside the recorded span leaves the span itself intact."""
    entity = _indexed_alpha(tmp_path)
    (tmp_path / "mod.py").write_text("import sys\n\n\ndef alpha():\n    return 1\n")

    assert LiveSourceLoader(tmp_path).load_verified_source(entity) is not None


def test_verified_source_fails_closed_on_a_deleted_file(tmp_path: Path) -> None:
    entity = _indexed_alpha(tmp_path)
    (tmp_path / "mod.py").unlink()

    assert LiveSourceLoader(tmp_path).load_verified_source(entity) is None


def test_verified_source_fails_closed_without_a_digest(tmp_path: Path) -> None:
    entity = _indexed_alpha(tmp_path)
    entity.metadata.pop("content_hash")

    assert LiveSourceLoader(tmp_path).load_verified_source(entity) is None


def test_verified_source_is_quietly_none_for_snippetless_entities(
    tmp_path: Path, caplog
) -> None:
    """MODULE/DOCUMENT-style rows hash identity fields, not text; their None
    is parity with the empty stored copy, not drift, and must not warn."""
    import logging

    from knowcode.utils.entity_identity import compute_entity_fallback_hash

    file_path = tmp_path / "mod.py"
    file_path.write_text("# a module docstring\nimport os\n")
    entity = Entity(
        id="mod.py::module",
        kind=EntityKind.MODULE,
        name="mod",
        qualified_name="mod",
        location=Location(file_path="mod.py", line_start=1, line_end=2),
        source_code=None,
    )
    entity.metadata["content_hash"] = compute_entity_fallback_hash(entity)

    with caplog.at_level(logging.WARNING):
        assert LiveSourceLoader(tmp_path).load_verified_source(entity) is None

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
