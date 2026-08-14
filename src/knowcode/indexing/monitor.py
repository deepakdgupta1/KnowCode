"""File system monitor for live re-indexing."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING, Any

from knowcode.indexing.scanner import Scanner
from knowcode.indexing.watch_queue import WatchQueueClosed
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


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

    def __init__(
        self,
        root_dir: str | Path,
        background_indexer: Optional["BackgroundIndexer"] = None,
    ) -> None:
        """Initialize a file system monitor.

        Args:
            root_dir: Root directory to watch.
            background_indexer: Optional background indexer to notify on changes.
        """
        self.root_dir = Path(root_dir)
        self.background_indexer = background_indexer
        self.observer: Any = None

    def start(self) -> bool:
        """Start watching the directory for changes.

        Returns:
            Whether this call started an observer. A second call is a no-op:
            scheduling two observers on one tree doubles every event and leaks
            the first, since :meth:`stop` can only join one.
        """
        if not Observer:
            logger.warning("watchdog is not installed; watch mode is disabled.")
            return False
        if self.observer is not None:
            return False

        event_handler = IndexingHandler(self.background_indexer, self.root_dir)
        self.observer = Observer()
        self.observer.schedule(event_handler, str(self.root_dir), recursive=True)
        self.observer.start()
        return True

    def stop(self) -> None:
        """Stop watching and join the observer thread. Idempotent."""
        observer, self.observer = self.observer, None
        if observer:
            observer.stop()
            observer.join()


class IndexingHandler(FileSystemEventHandler):
    """Turns file system events into watch-queue work items.

    Two rules keep the watched index identical to a built one:

    * A path is queued only when :meth:`Scanner.is_indexable` accepts it, so an
      edit under ``node_modules/`` or ``.git/`` is ignored here exactly as it
      is during a build.
    * Every path is queued as-is and canonicalized by the queue (ADR 1), so no
      alias of one file becomes two work items.
    """

    def __init__(
        self,
        background_indexer: Optional["BackgroundIndexer"],
        root_dir: str | Path,
    ) -> None:
        """Initialize the handler.

        Args:
            background_indexer: Worker responsible for indexing changed files.
            root_dir: Watched root, used to apply the scanner's ignore rules.
        """
        self.background_indexer = background_indexer
        self.scanner = Scanner(root_dir)

    def on_modified(self, event: Any) -> None:
        """Handle modified file events."""
        if not event.is_directory:
            self._queue_index(event.src_path)

    def on_created(self, event: Any) -> None:
        """Handle created file events."""
        if not event.is_directory:
            self._queue_index(event.src_path)

    def on_deleted(self, event: Any) -> None:
        """Handle deleted file events."""
        worker = self.background_indexer
        if event.is_directory or worker is None:
            return
        path = Path(event.src_path)
        if self.scanner.is_indexable(path):
            # queue_removal is part of the worker's interface (Step 15), so
            # a deletion is never re-routed to an index command.
            self._submit(worker.queue_removal, path)

    def on_moved(self, event: Any) -> None:
        """Handle moved file events."""
        worker = self.background_indexer
        if event.is_directory or worker is None:
            return
        src_path = Path(event.src_path)
        dest_path = Path(event.dest_path)
        src_indexable = self.scanner.is_indexable(src_path)
        dest_indexable = self.scanner.is_indexable(dest_path)

        if src_indexable and dest_indexable:
            # One work item, so the destination is proven before the source is
            # dropped and a chained rename collapses into a single commit.
            self._submit(worker.queue_move, src_path, dest_path)
        elif src_indexable:
            # Renamed out of the index — into an ignored directory, or to an
            # extension nothing parses.
            self._submit(worker.queue_removal, src_path)
        elif dest_indexable:
            self._queue_index(event.dest_path)

    def _queue_index(self, path_str: str) -> None:
        """Queue a file for indexing if a build would have included it."""
        worker = self.background_indexer
        if worker is None:
            return
        path = Path(path_str)
        if self.scanner.is_indexable(path):
            self._submit(worker.queue_file, path)

    @staticmethod
    def _submit(enqueue: Callable[..., None], *paths: Path) -> None:
        """Enqueue one event, tolerating a worker that is shutting down.

        The worker rejects work after ``stop()`` rather than accepting and
        dropping it. That rejection must not escape into watchdog's observer
        thread, where it would kill the watcher for every other file — and
        neither may anything else, so an unexpected error is logged with its
        traceback instead of being either swallowed or allowed to propagate.
        """
        try:
            enqueue(*paths)
        except WatchQueueClosed:
            logger.info(
                "Ignoring a file event for %s: the indexer is shutting down",
                paths[0],
            )
        except Exception:  # noqa: BLE001 - one bad event must not stop the watcher
            logger.exception("Could not queue a file event for %s", paths[0])
