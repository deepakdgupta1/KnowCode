"""Background indexing service."""

import queue
import threading
from pathlib import Path
from typing import Optional

from knowcode.indexing.indexer import Indexer


class BackgroundIndexer:
    """Runs indexing in background thread."""

    def __init__(self, indexer: Indexer) -> None:
        """Initialize the background worker with an Indexer instance.

        Args:
            indexer: Indexer used to process queued files.
        """
        self.indexer = indexer
        self._queue: queue.Queue = queue.Queue()  # type: ignore
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Start background indexing thread."""
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop background indexing."""
        self._running = False
        self._queue.put(None)  # Sentinel to unblock
        if self._thread:
            self._thread.join(timeout=5.0)

    def queue_file(self, path: Path) -> None:
        """Queue a file for indexing.

        Args:
            path: File path to enqueue for processing.
        """
        self._queue.put(("index", path))

    def queue_removal(self, path: Path) -> None:
        """Queue a file for removal from the index.

        Args:
            path: File path to remove.
        """
        self._queue.put(("remove", path))

    def queue_move(self, old_path: Path, new_path: Path) -> None:
        """Queue a file move/rename.

        Args:
            old_path: Original file path.
            new_path: New file path.
        """
        self._queue.put(("move", (old_path, new_path)))

    def _worker(self) -> None:
        """Worker thread that processes the indexing queue."""
        while self._running:
            try:
                # Use timeout to allow checking self._running
                item = self._queue.get(timeout=1.0)
                if item is None:
                    self._queue.task_done()
                    break

                # Support both legacy Path-only items and newer command tuples
                if isinstance(item, tuple):
                    cmd, args = item
                else:
                    cmd, args = "index", item

                # Every command is one prepare/commit transaction (Step 15).
                # Removing first would delete the previous generation before
                # its replacement was proven, which is how a transient
                # embedding failure used to erase a file from the index.
                if cmd == "index":
                    self.indexer.replace_file(args)
                elif cmd == "remove":
                    self.indexer.delete_file(args)
                elif cmd == "move":
                    old_path, new_path = args
                    self.indexer.move_file(old_path, new_path)

                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                import logging
                logging.error(f"Background indexer error: {e}")
                self._queue.task_done()

