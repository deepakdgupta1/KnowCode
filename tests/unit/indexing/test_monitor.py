"""Unit tests for the file system monitor."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.indexing.monitor import FileMonitor, IndexingHandler
from knowcode.indexing.scanner import Scanner
from knowcode.indexing.watch_queue import WatchQueueClosed, WatchWork


class MockBackgroundIndexer:
    def __init__(self, closed: bool = False) -> None:
        self.queued_files: list[Path] = []
        self.removed_files: list[Path] = []
        self.moved_files: list[tuple[Path, Path]] = []
        self.closed = closed

    def _check(self, path: Path) -> None:
        if self.closed:
            raise WatchQueueClosed(WatchWork.index(path))

    def queue_file(self, path: Path) -> None:
        self._check(path)
        self.queued_files.append(path)

    def queue_removal(self, path: Path) -> None:
        self._check(path)
        self.removed_files.append(path)

    def queue_move(self, old_path: Path, new_path: Path) -> None:
        self._check(new_path)
        self.moved_files.append((old_path, new_path))


class FakeEvent:
    def __init__(
        self, src_path: str, dest_path: str = "", is_directory: bool = False
    ) -> None:
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


@pytest.fixture
def watched(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A handler over a real watched root, since ignore rules need one."""
    bg = MockBackgroundIndexer()
    return bg, IndexingHandler(bg, tmp_path), tmp_path


def test_monitor_extension_filter_matches_scanner(watched) -> None:  # type: ignore[no-untyped-def]
    """Every extension a scan would yield must also be queued by the monitor."""
    bg, handler, root = watched

    for ext in Scanner.SUPPORTED_EXTENSIONS:
        bg.queued_files.clear()
        target = root / f"file{ext}"
        handler.on_modified(FakeEvent(str(target)))

        assert bg.queued_files == [target], f"Extension {ext} was not queued by monitor"


def test_monitor_matches_the_scanner_on_uppercase_extensions(watched) -> None:  # type: ignore[no-untyped-def]
    """`scan()` lowercases before comparing; the watch path has to agree."""
    bg, handler, root = watched

    handler.on_modified(FakeEvent(str(root / "Legacy.PY")))

    assert bg.queued_files == [root / "Legacy.PY"]


def test_monitor_on_created_queues_file(watched) -> None:  # type: ignore[no-untyped-def]
    """Test that on_created queues the file for indexing."""
    bg, handler, root = watched

    handler.on_created(FakeEvent(str(root / "file.py")))

    assert bg.queued_files == [root / "file.py"]
    assert not bg.removed_files
    assert not bg.moved_files


def test_monitor_on_modified_queues_file(watched) -> None:  # type: ignore[no-untyped-def]
    """Test that on_modified queues the file for indexing."""
    bg, handler, root = watched

    handler.on_modified(FakeEvent(str(root / "file.py")))

    assert bg.queued_files == [root / "file.py"]
    assert not bg.removed_files
    assert not bg.moved_files


def test_monitor_on_deleted_queues_removal(watched) -> None:  # type: ignore[no-untyped-def]
    """Test that on_deleted queues the file for removal."""
    bg, handler, root = watched

    handler.on_deleted(FakeEvent(str(root / "file.py")))

    assert bg.removed_files == [root / "file.py"]
    assert not bg.queued_files
    assert not bg.moved_files


def test_monitor_on_moved_queues_move(watched) -> None:  # type: ignore[no-untyped-def]
    """Test that on_moved queues the file move."""
    bg, handler, root = watched

    handler.on_moved(
        FakeEvent(str(root / "old_file.py"), dest_path=str(root / "new_file.py"))
    )

    assert bg.moved_files == [(root / "old_file.py", root / "new_file.py")]
    assert not bg.queued_files
    assert not bg.removed_files


def test_directory_events_are_ignored(watched) -> None:  # type: ignore[no-untyped-def]
    bg, handler, root = watched
    directory = FakeEvent(
        str(root / "pkg"), dest_path=str(root / "pkg2"), is_directory=True
    )

    handler.on_created(directory)
    handler.on_modified(directory)
    handler.on_deleted(directory)
    handler.on_moved(directory)

    assert not bg.queued_files and not bg.removed_files and not bg.moved_files


# ----------------------------------------------------------------------
# The watch path indexes exactly what a build would (Step 16)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "node_modules/dep.js",
        ".git/hooks/run.py",
        ".venv/lib/mod.py",
        "__pycache__/mod.py",
        "build/out.js",
    ],
)
def test_ignored_paths_are_never_queued(tmp_path: Path, relative: str) -> None:
    """The reviewed defect: the monitor filtered on extension alone, so an edit
    under an ignored directory entered the index even though no build would
    ever have included it."""
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, tmp_path)

    handler.on_created(FakeEvent(str(target)))
    handler.on_modified(FakeEvent(str(target)))
    handler.on_deleted(FakeEvent(str(target)))

    assert bg.queued_files == []
    assert bg.removed_files == []


def test_ignored_paths_are_never_queued_matches_a_real_scan(tmp_path: Path) -> None:
    """Both sides of the same question, answered by the same rules."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    scanned = {info.path for info in Scanner(tmp_path).scan_all()}
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, tmp_path)
    for candidate in (
        tmp_path / "node_modules" / "dep.js",
        tmp_path / "src" / "app.py",
    ):
        handler.on_modified(FakeEvent(str(candidate)))

    assert set(bg.queued_files) == scanned


def test_a_symlinked_source_file_is_watched_because_a_scan_yields_it(
    tmp_path: Path,
) -> None:
    """`scan()` walks without resolving, so a link inside the root is indexed."""
    root, outside = tmp_path / "repo", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "target.py"
    target.write_text("x = 1\n", encoding="utf-8")
    link = root / "link.py"
    link.symlink_to(target)

    scanned = {info.path for info in Scanner(root).scan_all()}
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, root)
    handler.on_modified(FakeEvent(str(link)))

    assert link in scanned
    assert bg.queued_files == [link]


def test_an_aliased_root_still_matches(tmp_path: Path) -> None:
    """An event may carry an unresolved spelling of the root itself."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, real)

    handler.on_modified(FakeEvent(str(alias / "app.py")))

    assert bg.queued_files == [alias / "app.py"]


def test_an_unexpected_queue_error_does_not_break_the_observer_thread(
    tmp_path: Path,
) -> None:
    """One bad event must not stop the watcher for every other file."""

    class _Exploding(MockBackgroundIndexer):
        def queue_file(self, path: Path) -> None:
            raise RuntimeError("the queue is broken")

    handler = IndexingHandler(_Exploding(), tmp_path)  # type: ignore[arg-type]

    handler.on_modified(FakeEvent(str(tmp_path / "a.py")))


def test_paths_outside_the_watched_root_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, root)

    handler.on_modified(FakeEvent(str(tmp_path / "elsewhere.py")))

    assert bg.queued_files == []


def test_a_move_out_of_the_index_becomes_a_removal(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, tmp_path)

    handler.on_moved(
        FakeEvent(
            str(tmp_path / "app.py"),
            dest_path=str(tmp_path / "node_modules" / "app.py"),
        )
    )

    assert bg.removed_files == [tmp_path / "app.py"]
    assert not bg.moved_files


def test_a_move_into_the_index_becomes_an_index(tmp_path: Path) -> None:
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, tmp_path)

    handler.on_moved(
        FakeEvent(str(tmp_path / "notes.txt"), dest_path=str(tmp_path / "notes.md"))
    )

    assert bg.queued_files == [tmp_path / "notes.md"]
    assert not bg.moved_files and not bg.removed_files


def test_a_move_between_unindexed_paths_is_ignored(tmp_path: Path) -> None:
    bg = MockBackgroundIndexer()
    handler = IndexingHandler(bg, tmp_path)

    handler.on_moved(
        FakeEvent(str(tmp_path / "a.txt"), dest_path=str(tmp_path / "b.txt"))
    )

    assert not bg.queued_files and not bg.removed_files and not bg.moved_files


def test_events_during_shutdown_do_not_break_the_observer_thread(
    tmp_path: Path,
) -> None:
    """A rejected event is logged; raising here would kill the whole watcher."""
    bg = MockBackgroundIndexer(closed=True)
    handler = IndexingHandler(bg, tmp_path)

    handler.on_modified(FakeEvent(str(tmp_path / "a.py")))
    handler.on_deleted(FakeEvent(str(tmp_path / "a.py")))
    handler.on_moved(
        FakeEvent(str(tmp_path / "a.py"), dest_path=str(tmp_path / "b.py"))
    )

    assert bg.queued_files == [] and bg.removed_files == [] and bg.moved_files == []


def test_a_handler_without_a_worker_raises_nothing(tmp_path: Path) -> None:
    """`FileMonitor` allows a null worker; every entry point must tolerate it."""
    handler = IndexingHandler(None, tmp_path)

    handler.on_modified(FakeEvent(str(tmp_path / "a.py")))
    handler.on_deleted(FakeEvent(str(tmp_path / "a.py")))
    handler.on_moved(
        FakeEvent(str(tmp_path / "a.py"), dest_path=str(tmp_path / "b.py"))
    )

    assert handler.background_indexer is None


# ----------------------------------------------------------------------
# Monitor lifecycle
# ----------------------------------------------------------------------


def test_starting_the_monitor_twice_schedules_one_observer(tmp_path: Path) -> None:
    """Two observers on one tree double every event and leak the first."""
    monitor = FileMonitor(tmp_path, MockBackgroundIndexer())  # type: ignore[arg-type]

    try:
        assert monitor.start() is True
        first = monitor.observer
        assert monitor.start() is False
        assert monitor.observer is first
    finally:
        monitor.stop()

    assert monitor.observer is None


def test_stopping_the_monitor_is_idempotent(tmp_path: Path) -> None:
    monitor = FileMonitor(tmp_path, MockBackgroundIndexer())  # type: ignore[arg-type]
    monitor.start()

    monitor.stop()
    monitor.stop()

    assert monitor.observer is None
