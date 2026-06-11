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
        source_code="old line 2\nold line 3\n"
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
        source_code="old code"
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
        source_code="old code"
    )
    
    loader = LiveSourceLoader(tmp_path)
    source = loader.load_source(entity)
    assert source is None
