"""Unit tests for the file system monitor."""

from pathlib import Path
from knowcode.indexing.monitor import IndexingHandler
from knowcode.indexing.scanner import Scanner

class MockBackgroundIndexer:
    def __init__(self) -> None:
        self.queued_files: list[Path] = []
        self.removed_files: list[Path] = []
        self.moved_files: list[tuple[Path, Path]] = []

    def queue_file(self, path: Path) -> None:
        self.queued_files.append(path)

    def queue_removal(self, path: Path) -> None:
        self.removed_files.append(path)

    def queue_move(self, old_path: Path, new_path: Path) -> None:
        self.moved_files.append((old_path, new_path))


class FakeEvent:
    def __init__(self, src_path: str, dest_path: str = "", is_directory: bool = False) -> None:
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


def test_monitor_extension_filter_matches_scanner() -> None:
    """Test that IndexingHandler filters extensions exactly matching Scanner.SUPPORTED_EXTENSIONS."""
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg)  # type: ignore

    # Test all supported extensions
    for ext in Scanner.SUPPORTED_EXTENSIONS:
        bg.queued_files.clear()
        event = FakeEvent(f"file{ext}")
        handler.on_modified(event)
        
        assert len(bg.queued_files) == 1, f"Extension {ext} was not queued by monitor"
        assert bg.queued_files[0] == Path(f"file{ext}")


def test_monitor_on_created_queues_file() -> None:
    """Test that on_created queues the file for indexing."""
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg)  # type: ignore

    event = FakeEvent("file.py")
    handler.on_created(event)

    assert bg.queued_files == [Path("file.py")]
    assert not bg.removed_files
    assert not bg.moved_files


def test_monitor_on_modified_queues_file() -> None:
    """Test that on_modified queues the file for indexing."""
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg)  # type: ignore

    event = FakeEvent("file.py")
    handler.on_modified(event)

    assert bg.queued_files == [Path("file.py")]
    assert not bg.removed_files
    assert not bg.moved_files


def test_monitor_on_deleted_queues_removal() -> None:
    """Test that on_deleted queues the file for removal."""
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg)  # type: ignore

    event = FakeEvent("file.py")
    handler.on_deleted(event)

    assert bg.removed_files == [Path("file.py")]
    assert not bg.queued_files
    assert not bg.moved_files


def test_monitor_on_moved_queues_move() -> None:
    """Test that on_moved queues the file move."""
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg)  # type: ignore

    event = FakeEvent("old_file.py", dest_path="new_file.py")
    handler.on_moved(event)

    assert bg.moved_files == [(Path("old_file.py"), Path("new_file.py"))]
    assert not bg.queued_files
    assert not bg.removed_files
