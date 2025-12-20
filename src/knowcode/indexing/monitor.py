"""File system monitor for live re-indexing."""

import time
from pathlib import Path
from typing import Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = None
    FileSystemEventHandler = object


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
        self.observer = None

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

    def on_modified(self, event):
        """Handle modified file events."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_created(self, event):
        """Handle created file events."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def _handle_change(self, path_str: str) -> None:
        """Queue a file for indexing if it is a supported source type."""
        if self.background_indexer:
            path = Path(path_str)
            # Basic extension filtering
            if path.suffix in {".py", ".js", ".ts", ".java", ".md", ".yml", ".yaml"}:
                self.background_indexer.queue_file(path)
