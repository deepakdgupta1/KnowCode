"""Background indexing service."""

import queue
import threading
from pathlib import Path
from typing import Optional

from knowcode.indexer import Indexer


class BackgroundIndexer:
    """Runs indexing in background thread."""

    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer
        self._queue: queue.Queue = queue.Queue()
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
        """Queue a file for indexing."""
        self._queue.put(path)

    def _worker(self) -> None:
        """Worker thread that processes indexing queue."""
        while self._running:
            try:
                # Use timeout to allow checking self._running
                path = self._queue.get(timeout=1.0)
                if path is None:
                    break
                self.indexer.index_file(path)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                # Log error but continue
                # In production, use high-fidelity logging
                import logging
                logging.error(f"Background indexer error: {e}")
                self._queue.task_done()
