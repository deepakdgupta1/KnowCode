"""Background indexing worker for watched file events (Step 16).

The worker owns one thread that drains a :class:`~knowcode.indexing.watch_queue.
WatchQueue` by committing each item as a Step 15 generation transaction. Its
job is to make a stream of filesystem events converge on the index the final
filesystem state implies — losing nothing on the way, and saying so when it
cannot.

Three defects reproduced against the pre-Step-16 worker, using its production
API only:

* Five modify events for one file committed five transactions, and two
  spellings of one path committed two. Coalescing now happens in the queue.
* ``start()`` twice ran two worker threads over one queue, so two commits for
  two files ran concurrently and ``stop()`` joined only the last thread.
* With three items queued behind an in-flight commit, ``stop()`` committed one
  and dropped the other two, returning ``None``. A provider outage dropped its
  update just as quietly: no retry, and nothing exposed the loss.

The contract now:

* **Idempotent lifecycle.** ``start()`` reports whether it started the worker;
  a second call is a no-op. ``stop()`` is bounded, drains what it can, and
  returns a :class:`DrainReport` naming everything it could not commit.
* **Bounded retries.** A retryable failure — a provider outage, a store that
  needs re-deriving — is retried with backoff up to ``max_attempts``. A
  terminal failure is not retried. Neither is ever silent: both preserve the
  file's previous generation (Step 15) and land in :meth:`failures`.
* **Explicit rejection.** Work submitted after ``stop()`` raises rather than
  being accepted and dropped.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from knowcode.indexing.file_updates import FileUpdateError
from knowcode.indexing.watch_queue import WatchOp, WatchQueue, WatchWork
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Backoff before each retry. ``retry_delays[i]`` precedes attempt ``i + 2``,
#: so the default pairs with ``max_attempts=3``. Deliberately short: a watch
#: worker that stalls for minutes is a worse failure than one that gives up
#: visibly and waits for the next save.
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0)

#: How many terminal/exhausted failures to keep for inspection. A long-running
#: watch on a broken tree must not grow a list forever.
FAILURE_HISTORY = 100

#: Liveness backstop for a consumer waiting on the queue. Correctness comes
#: from the queue's condition variables; this only bounds a missed wakeup.
_TAKE_TIMEOUT = 0.5


class WatchIndexer(Protocol):
    """The two commit operations a watch worker drives.

    Narrower than :class:`~knowcode.indexing.indexer.Indexer` on purpose. The
    worker used to be handed one indexer bound to one generation and hold it
    forever; since Step 18 the server passes a
    :class:`~knowcode.service_watch.ServiceWatchWriter`, which resolves the
    current generation under a lease per commit. Both satisfy this protocol, so
    the worker never has to know which it got.
    """

    def replace_file(self, file_path: str | Path) -> Any:
        """Replace one file's chunks and vectors as one transaction."""

    def delete_file(self, file_path: str | Path) -> Any:
        """Remove one file's chunks and vectors as one transaction."""


@runtime_checkable
class WatchPublisher(Protocol):
    """A writer whose commits become visible only when it publishes (Step 18b).

    Separate from :class:`WatchIndexer` because publication is optional: a
    plain :class:`~knowcode.indexing.indexer.Indexer` writes into a mutable
    index directory and has nothing to publish, while
    :class:`~knowcode.service_watch.ServiceWatchWriter` stages a successor
    generation that no reader can see until it is published.

    The worker drives it because the *queue* is what knows a burst has ended.
    Committing per event but publishing per drain is what keeps one save from
    costing a full staging copy per keystroke.
    """

    def publish_pending(self) -> Optional[str]:
        """Publish staged commits, returning the new generation id or ``None``."""

    def pending_paths(self) -> tuple[str, ...]:
        """File identities that are committed but not yet published."""


@dataclass(frozen=True)
class WatchFailure:
    """Work the queue accepted but could not commit.

    Recorded only for final outcomes. A retryable failure that later succeeds
    is logged, not recorded, so this list means "not in the index" rather than
    "hit a bump on the way in".
    """

    path: str
    operation: str
    attempts: int
    reason: str
    retryable: bool
    #: Identities this item was going to drop and did not — a failed rename
    #: leaves its *source* in the index, so naming only the destination would
    #: under-report what is stale.
    dropped_paths: tuple[str, ...] = ()

    @property
    def exhausted(self) -> bool:
        """Whether this failure ran out of attempts rather than being terminal."""
        return self.retryable


@dataclass(frozen=True)
class DrainReport:
    """What a bounded drain achieved, and what it left behind."""

    completed: bool
    pending: tuple[WatchWork, ...] = ()
    in_flight: Optional[WatchWork] = None
    failures: tuple[WatchFailure, ...] = ()
    #: Committed into a staging generation but never published, so no reader
    #: will ever see it and a restart will not find it (Step 18b). Committed is
    #: not durable, and this is where the difference is stated.
    unpublished: tuple[str, ...] = ()

    @property
    def incomplete_work(self) -> tuple[str, ...]:
        """Every path that is not in the index as its events implied."""
        paths: list[str] = []
        for work in self.pending:
            paths.append(work.path)
            paths.extend(work.dropped_paths)
        if self.in_flight is not None:
            paths.append(self.in_flight.path)
            paths.extend(self.in_flight.dropped_paths)
        for failure in self.failures:
            paths.append(failure.path)
            paths.extend(failure.dropped_paths)
        paths.extend(self.unpublished)
        return tuple(dict.fromkeys(paths))


class BackgroundIndexer:
    """Commits watched file events as Step 15 generation transactions."""

    def __init__(
        self,
        indexer: WatchIndexer,
        *,
        max_attempts: int = 3,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize the background worker.

        Args:
            indexer: Writer whose ``replace_file``/``delete_file`` transactions
                commit each work item. The server passes a
                :class:`~knowcode.service_watch.ServiceWatchWriter` so commits
                follow generation swaps; a direct ``Indexer`` also satisfies
                the protocol for single-generation callers and tests.
            max_attempts: Total attempts for one work item, retryable failures
                included. ``1`` disables retries.
            retry_delays: Backoff before each retry. The last value repeats if
                there are more attempts than delays.
            sleep: Injectable sleep so retry timing is asserted rather than
                waited out (the plan's global rule against timing-only tests).
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.indexer = indexer
        # A writer that stages its commits also has to be told when to publish;
        # one that writes in place does not. Resolved once, so the per-commit
        # path is a null check rather than a protocol test.
        self.publisher: Optional[WatchPublisher] = (
            indexer if isinstance(indexer, WatchPublisher) else None
        )
        self.max_attempts = max_attempts
        self.retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._queue = WatchQueue()
        self._thread: Optional[threading.Thread] = None
        self._lifecycle = threading.Lock()
        self._failures: deque[WatchFailure] = deque(maxlen=FAILURE_HISTORY)
        self._failures_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Start the worker thread.

        Returns:
            Whether this call started it. ``False`` means one is already
            running, which is a no-op rather than a second consumer: two
            threads over one queue commit two files at once and only the last
            is ever joined.
        """
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._queue.closed:
                raise RuntimeError(
                    "This BackgroundIndexer was stopped; create a new one."
                )
            self._thread = threading.Thread(
                target=self._worker, name="knowcode-watch-indexer", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> DrainReport:
        """Stop accepting work, drain what is queued, and report the rest.

        Args:
            timeout: Upper bound on the whole drain. Shutdown is bounded even
                when a commit hangs or a retry is backing off.

        Returns:
            A report whose ``completed`` flag is true only when every accepted
            item committed. Anything else is named in ``pending``,
            ``in_flight``, and ``failures`` rather than being claimed as
            durable.
        """
        deadline = time.monotonic() + timeout
        with self._lifecycle:
            thread = self._thread
            self._queue.close()

        drained = self._queue.join(timeout)
        stopped = True
        if thread is not None:
            # One deadline for the whole call, so a hung commit cannot make
            # shutdown take twice the timeout it was given.
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            stopped = not thread.is_alive()
            with self._lifecycle:
                if self._thread is thread and stopped:
                    self._thread = None

        # The worker publishes each drain itself, so this normally finds
        # nothing. It runs anyway for the cases where the worker never got
        # there: a thread that was never started, one that timed out mid-batch,
        # or a batch whose earlier publication failed.
        self._publish_pending()
        return self._report(completed=drained and stopped and not self.failures())

    def drain(self, timeout: float = 5.0) -> DrainReport:
        """Wait for the queue to empty without stopping the worker."""
        return self._report(
            completed=self._queue.join(timeout) and not self.failures()
        )

    def join(self, timeout: float = 5.0) -> bool:
        """Wait for the worker thread to terminate.

        Unlike :meth:`stop`, this does not close the queue or drain work —
        it only waits for the thread to exit. Useful when external test
        scaffolding unblocks the thread after a timed-out shutdown, and the
        caller needs to guarantee the thread is gone before the next test.

        Args:
            timeout: Upper bound on the wait.

        Returns:
            Whether the thread terminated within the timeout.
        """
        with self._lifecycle:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    @property
    def is_running(self) -> bool:
        """Whether a worker thread is alive and consuming."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _report(self, *, completed: bool) -> DrainReport:
        unpublished = self.unpublished()
        return DrainReport(
            completed=completed and not unpublished,
            pending=self._queue.pending(),
            in_flight=self._queue.in_flight,
            failures=self.failures(),
            unpublished=unpublished,
        )

    def unpublished(self) -> tuple[str, ...]:
        """File identities committed into a staging generation but not published."""
        if self.publisher is None:
            return ()
        return tuple(self.publisher.pending_paths())

    # -- submission ------------------------------------------------------

    def queue_file(self, path: str | Path) -> None:
        """Queue a created or modified file for replacement.

        Raises:
            WatchQueueClosed: The worker is shutting down.
        """
        self._queue.submit(WatchWork.index(path))

    def queue_removal(self, path: str | Path) -> None:
        """Queue a deleted file's removal from the index.

        Raises:
            WatchQueueClosed: The worker is shutting down.
        """
        self._queue.submit(WatchWork.delete(path))

    def queue_move(self, old_path: str | Path, new_path: str | Path) -> None:
        """Queue a rename as one transaction: index the destination, drop the source.

        Raises:
            WatchQueueClosed: The worker is shutting down.
        """
        self._queue.submit(WatchWork.index(new_path, moved_from=old_path))

    # -- inspection ------------------------------------------------------

    def failures(self) -> tuple[WatchFailure, ...]:
        """Work that is not in the index, newest last."""
        with self._failures_lock:
            return tuple(self._failures)

    def pending(self) -> tuple[WatchWork, ...]:
        """Uncommitted work, in submission order."""
        return self._queue.pending()

    # -- worker ----------------------------------------------------------

    def _worker(self) -> None:
        """Commit queued work until the queue is closed and drained."""
        while True:
            work = self._queue.take(timeout=_TAKE_TIMEOUT)
            if work is None:
                if self._queue.closed and self._queue.is_idle():
                    return
                continue
            try:
                self._process(work)
            except Exception:  # noqa: BLE001 - the thread must outlive one item
                # Only reachable if failure *handling* itself failed. Dying
                # here would be the worst outcome available: producers would
                # keep queueing work that nothing consumes, and nothing would
                # say so.
                logger.exception(
                    "The watch worker could not classify a failure for %s",
                    work.describes,
                )

    def _process(self, work: WatchWork) -> None:
        """Commit one item, publish if the burst has ended, and release it."""
        try:
            self._commit(work)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            self._handle_failure(work, exc)
        finally:
            try:
                self._publish_if_drained()
            finally:
                # Released last, so a caller whose ``join`` returns can rely on
                # the batch having been published rather than racing it.
                self._queue.complete(work)

    def _publish_if_drained(self) -> None:
        """Publish the staged batch once nothing is left to commit.

        Checked before the item is released, and after a retry has had its
        chance to requeue: an item waiting for another attempt means the burst
        has not ended, so its file is not published half-updated.
        """
        if self.publisher is None or self._queue.pending():
            return
        self._publish_pending()

    def _publish_pending(self) -> None:
        """Publish staged commits, reporting rather than propagating failures.

        A publication that fails must not kill the worker thread or turn a
        commit failure into an unhandled one. The batch stays named by
        ``publisher.pending_paths()``, which is what the drain report carries,
        so the loss is reported rather than swallowed.
        """
        if self.publisher is None:
            return
        try:
            self.publisher.publish_pending()
        except Exception:  # noqa: BLE001 - reported through the drain report
            logger.exception(
                "Could not publish the staged watched updates; they remain "
                "uncommitted to any generation"
            )

    def _commit(self, work: WatchWork) -> None:
        """Apply one work item as Step 15 generation transactions.

        The destination commits before any source is dropped — ``Indexer.
        move_file``'s ordering, generalized to the several sources a coalesced
        move chain carries. A failure here therefore leaves every source
        searchable rather than producing a file that exists under no identity.
        """
        if work.op is WatchOp.INDEX:
            self.indexer.replace_file(work.path)
        else:
            self.indexer.delete_file(work.path)
        for dropped in work.dropped_paths:
            self.indexer.delete_file(dropped)

    def _handle_failure(self, work: WatchWork, exc: BaseException) -> None:
        """Retry, or record the item as work that never made it in."""
        retryable = self._is_retryable(exc)
        if retryable and work.attempt < self.max_attempts:
            if self._queue.requeue(work):
                logger.warning(
                    "Retrying %s (attempt %d of %d): %s",
                    work.describes,
                    work.attempt + 1,
                    self.max_attempts,
                    exc,
                )
                self._backoff(work.attempt)
            else:
                logger.info(
                    "Dropping the retry for %s: a newer event supersedes it",
                    work.path,
                )
            return

        # Step 15 guarantees the file kept its previous generation, so the
        # index is stale here, never damaged.
        logger.error(
            "Giving up on %s after %d attempt(s); its previous index "
            "generation is unchanged: %s",
            work.describes,
            work.attempt,
            exc,
        )
        with self._failures_lock:
            self._failures.append(
                WatchFailure(
                    path=work.path,
                    operation=work.op.value,
                    attempts=work.attempt,
                    reason=str(exc),
                    retryable=retryable,
                    dropped_paths=work.dropped_paths,
                )
            )

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """Classify a commit failure.

        ``FileUpdateError`` carries its own classification (Step 15). An
        ``OSError`` is the file still being written or a store briefly locked,
        which is exactly what a retry is for. Anything else is unrecognized,
        so it fails closed as terminal and visible rather than being retried
        blindly.
        """
        if isinstance(exc, FileUpdateError):
            return exc.retryable
        return isinstance(exc, OSError)

    def _backoff(self, attempt: int) -> None:
        """Wait before the retry of ``attempt``, unless we are shutting down."""
        if self._queue.closed:
            # A bounded drain must not spend its budget sleeping. The attempt
            # cap still bounds the retries themselves.
            return
        if not self.retry_delays:
            return
        delay = self.retry_delays[min(attempt, len(self.retry_delays)) - 1]
        if delay > 0:
            self._sleep(delay)
