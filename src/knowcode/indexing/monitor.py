"""File system monitor for live re-indexing."""

from __future__ import annotations

from pathlib import Path
from knowcode.indexing.scanner import Scanner
from typing import Optional, TYPE_CHECKING, Any


try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = None  # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]

if TYPE_CHECKING:
    from knowcode.indexing.background_indexer import BackgroundIndexer


class FileMonitor:
    """Watches for file changes to trigger re-indexing."""

    def __init__(self, root_dir: str | Path, background_indexer: Optional["BackgroundIndexer"] = None) -> None:
        """Initialize a file system monitor.

        Args:
            root_dir: Root directory to watch.
            background_indexer: Optional background indexer to notify on changes.
        """
        self.root_dir = Path(root_dir)
        self.background_indexer = background_indexer
        self.observer: Any = None

    def start(self) -> None:
        """Start watching the directory for changes."""
        if not Observer:
            print("watchdog not installed. Watch mode disabled.")
            return

        event_handler = IndexingHandler(self.background_indexer)
        self.observer = Observer()
        self.observer.schedule(event_handler, str(self.root_dir), recursive=True)
        self.observer.start()

    def stop(self) -> None:
        """Stop watching and join the observer thread."""
        if self.observer:
            self.observer.stop()
            self.observer.join()


class IndexingHandler(FileSystemEventHandler):
    """Handles file system events for indexing."""

    def __init__(self, background_indexer: Optional["BackgroundIndexer"]) -> None:
        """Initialize the handler with an optional background indexer.

        Args:
            background_indexer: Worker responsible for indexing changed files.
        """
        self.background_indexer = background_indexer

    def on_modified(self, event: Any) -> None:
        """Handle modified file events."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_created(self, event: Any) -> None:
        """Handle created file events."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_deleted(self, event: Any) -> None:
        """Handle deleted file events."""
        if not event.is_directory and self.background_indexer:
            path = Path(event.src_path)
            if path.suffix in Scanner.SUPPORTED_EXTENSIONS:
                # queue_removal is part of the worker's interface (Step 15), so
                # a deletion is never re-routed to an index command.
                self.background_indexer.queue_removal(path)

    def on_moved(self, event: Any) -> None:
        """Handle moved file events."""
        if not event.is_directory and self.background_indexer:
            src_path = Path(event.src_path)
            dest_path = Path(event.dest_path)
            src_supported = src_path.suffix in Scanner.SUPPORTED_EXTENSIONS
            dest_supported = dest_path.suffix in Scanner.SUPPORTED_EXTENSIONS

            if src_supported and dest_supported:
                self.background_indexer.queue_move(src_path, dest_path)
            elif src_supported:
                self.background_indexer.queue_removal(src_path)
            elif dest_supported:
                self._handle_change(event.dest_path)

    def _handle_change(self, path_str: str) -> None:
        """Queue a file for indexing if it is a supported source type."""
        if self.background_indexer:
            path = Path(path_str)
            # Filter using Scanner's supported extensions
            if path.suffix in Scanner.SUPPORTED_EXTENSIONS:
                self.background_indexer.queue_file(path)


