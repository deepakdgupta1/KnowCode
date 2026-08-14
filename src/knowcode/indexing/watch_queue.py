"""Coalescing work queue for watched file events (Step 16).

A filesystem watcher is a firehose of duplicates: one editor save emits several
modify events, a rename emits a move whose destination is renamed again a moment
later, and a `git checkout` emits creates, modifies, and deletes for the same
path within milliseconds. The pre-Step-16 worker put every one of those on a
plain :class:`queue.Queue` and committed a full index transaction for each, keyed
by whatever string the event carried::

    queue_file("/tmp/repo/m.py")
    queue_file("/tmp/repo/sub/../m.py")   # the same file, a second work item

Reproduced against that code: a burst of five modify events committed five
transactions, and two spellings of one path committed two.

This module is the queue those events land in instead. Its contract:

* **One work item per canonical file identity.** Paths normalize through
  :func:`normalize_file_identity` (ADR 1) before they are keys, so a symlink,
  a ``..`` segment, and a ``/var`` vs ``/private/var`` alias are one item.
* **The last event for a path wins.** Events do not accumulate; they collapse
  into the single replacement that the file's final on-disk state implies.
  Committing that one item produces the same index as replaying every event.
* **Submission order is preserved.** Coalescing updates an item in place rather
  than moving it to the tail, so a rapidly-resaved file cannot starve the files
  queued behind it.
* **In-flight work never absorbs a new event.** A commit that is already
  running read the file before the new event happened, so the new event
  schedules a fresh item rather than being silently folded into a stale read.

A move is stored as its two effects — index the destination, drop the source —
which is what makes chains collapse: ``a → b`` followed by ``b → c`` becomes one
item that indexes ``c`` and drops both ``a`` and ``b``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Optional

from knowcode.utils.entity_identity import normalize_file_identity


class WatchOp(Enum):
    """What committing a work item does to its path's generation."""

    INDEX = "index"
    DELETE = "delete"


@dataclass(frozen=True)
class WatchWork:
    """One file's pending index transaction.

    Attributes:
        path: Canonical identity to index or delete.
        op: Whether the path is being replaced or removed.
        dropped_paths: Identities whose generation must be removed *after*
            ``path`` commits. A move contributes its source here, so the
            destination is proven before the source is dropped — the Step 15
            move ordering, generalized to the several sources a coalesced move
            chain accumulates.
        attempt: 1 for a newly observed event; incremented by a retry.
    """

    path: str
    op: WatchOp
    dropped_paths: tuple[str, ...] = ()
    attempt: int = 1

    @classmethod
    def index(cls, path: str | Path, *, moved_from: Optional[str | Path] = None) -> "WatchWork":
        """Build an index (replace) item, optionally as a move destination."""
        dropped: tuple[str, ...] = ()
        if moved_from is not None:
            source = normalize_file_identity(moved_from)
            destination = normalize_file_identity(path)
            # A move whose endpoints normalize to one identity is a replacement
            # with nothing to drop, not a self-deletion.
            dropped = () if source == destination else (source,)
        return cls(normalize_file_identity(path), WatchOp.INDEX, dropped)

    @classmethod
    def delete(cls, path: str | Path) -> "WatchWork":
        """Build a removal item."""
        return cls(normalize_file_identity(path), WatchOp.DELETE)

    @property
    def describes(self) -> str:
        """A short, non-sensitive description for logs and reports."""
        if self.dropped_paths:
            return f"{self.op.value} {self.path} (dropping {', '.join(self.dropped_paths)})"
        return f"{self.op.value} {self.path}"


class WatchQueueClosed(RuntimeError):
    """Raised when work is submitted to a queue that is shutting down.

    Explicit rather than silent: a watcher that keeps emitting events during
    shutdown must be told they will not be indexed, not left believing they
    were (indexing invariant 6).
    """

    def __init__(self, work: WatchWork) -> None:
        self.work = work
        super().__init__(
            f"The watch queue is closed; refusing to accept {work.describes}"
        )


class WatchQueue:
    """A coalescing, order-preserving queue keyed by canonical file identity.

    Safe for one consumer and any number of producers. The consumer takes an
    item, commits it, and calls :meth:`complete`; :meth:`join` reports when
    nothing is pending *or* in flight, which is what makes a bounded drain
    meaningful.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._work_available = threading.Condition(self._lock)
        self._idle = threading.Condition(self._lock)
        self._order: list[str] = []
        self._pending: dict[str, WatchWork] = {}
        self._in_flight: Optional[WatchWork] = None
        self._closed = False

    # -- producer side ---------------------------------------------------

    def submit(self, work: WatchWork) -> None:
        """Coalesce one event into the pending set.

        Raises:
            WatchQueueClosed: The queue is draining and cannot accept work.
        """
        with self._lock:
            if self._closed:
                raise WatchQueueClosed(work)
            self._submit_locked(work)

    def requeue(self, work: WatchWork) -> bool:
        """Schedule a retry of an item whose commit failed.

        A retry is a *replay*, which is the opposite of a fresh event: every
        pending item is newer than it, so unlike :meth:`submit` it never
        overrides one. It contributes only what nothing else owns — the drop
        obligations it was carrying, which :meth:`take` already removed from
        the pending set.

        Three rules follow from that, and each closes a way an index could go
        wrong:

        * A source with its own pending item is live again, or already handled
          by newer information. The replay must not delete it.
        * A pending item that already drops this path says the path is gone.
          The replay is obsolete; its remaining drops move to that item, so one
          commit still removes everything.
        * A pending item *for* this path re-reads the file. The replay's
          operation is redundant; again, only its drops survive.

        Returns:
            Whether the retry itself was scheduled. ``False`` means newer work
            has taken it over — never that anything was discarded.

        A closed queue still accepts retries: a drain is bounded by its
        caller's timeout, and abandoning an in-progress transaction at
        shutdown is exactly the loss this step exists to prevent.
        """
        with self._lock:
            dropped = {
                source
                for source in work.dropped_paths
                if source not in self._pending
            }
            owner = self._pending.get(work.path) or self._find_dropper(work.path)
            if owner is not None:
                merged = set(owner.dropped_paths) | dropped
                merged.discard(owner.path)
                self._pending[owner.path] = replace(
                    owner, dropped_paths=tuple(sorted(merged))
                )
                return False

            self._order.append(work.path)
            self._pending[work.path] = replace(
                work,
                dropped_paths=tuple(sorted(dropped)),
                attempt=work.attempt + 1,
            )
            self._work_available.notify()
            return True

    def _find_dropper(self, path: str) -> Optional[WatchWork]:
        """The pending item, if any, that already drops ``path``."""
        for work in self._pending.values():
            if path in work.dropped_paths:
                return work
        return None

    def _submit_locked(self, work: WatchWork) -> None:
        """Merge ``work`` into the pending set. Caller holds the lock."""
        if work.op is WatchOp.INDEX:
            # The path exists again, so no earlier move may drop it. Without
            # this, `move(a, b)` followed by re-creating `a` would delete the
            # new `a` whenever the move happened to commit second.
            self._forget_drop(work.path)

        dropped = set(work.dropped_paths)
        for source in work.dropped_paths:
            superseded = self._pending.pop(source, None)
            if superseded is not None:
                # A move chain: the pending item for `b` is replaced by this
                # item for `c`, and `b`'s own sources are inherited so the
                # whole chain is dropped by one commit.
                self._order.remove(source)
                dropped.update(superseded.dropped_paths)

        existing = self._pending.get(work.path)
        if existing is None:
            self._order.append(work.path)
        else:
            # Last event wins for the operation, but a pending move's sources
            # still have to be dropped: a modify after a rename must not leave
            # the old identity indexed.
            dropped.update(existing.dropped_paths)

        dropped.discard(work.path)
        self._pending[work.path] = replace(
            work, dropped_paths=tuple(sorted(dropped))
        )
        self._work_available.notify()

    def _forget_drop(self, path: str) -> None:
        """Stop any pending item from dropping ``path``. Caller holds the lock."""
        for key, work in self._pending.items():
            if path in work.dropped_paths:
                self._pending[key] = replace(
                    work,
                    dropped_paths=tuple(p for p in work.dropped_paths if p != path),
                )

    # -- consumer side ---------------------------------------------------

    def take(self, timeout: Optional[float] = None) -> Optional[WatchWork]:
        """Claim the oldest pending item, or ``None`` if none arrives in time."""
        with self._work_available:
            if not self._order:
                self._work_available.wait_for(
                    lambda: bool(self._order) or self._closed, timeout
                )
            if not self._order:
                return None
            path = self._order.pop(0)
            work = self._pending.pop(path)
            self._in_flight = work
            return work

    def complete(self, work: WatchWork) -> None:
        """Release the in-flight item, whatever its outcome."""
        with self._idle:
            if self._in_flight is work:
                self._in_flight = None
            if self._is_idle_locked():
                self._idle.notify_all()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is pending or in flight.

        Returns:
            Whether the queue reached that state within ``timeout``.
        """
        with self._idle:
            return bool(self._idle.wait_for(self._is_idle_locked, timeout))

    # -- lifecycle and inspection ----------------------------------------

    def close(self) -> None:
        """Refuse new work and wake any consumer waiting for it."""
        with self._lock:
            self._closed = True
            self._work_available.notify_all()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def in_flight(self) -> Optional[WatchWork]:
        with self._lock:
            return self._in_flight

    def is_idle(self) -> bool:
        """Whether nothing is pending or in flight right now."""
        with self._lock:
            return self._is_idle_locked()

    def _is_idle_locked(self) -> bool:
        return not self._order and self._in_flight is None

    def pending(self) -> tuple[WatchWork, ...]:
        """Snapshot the uncommitted work, in submission order."""
        with self._lock:
            return tuple(self._pending[path] for path in self._order)

    def __len__(self) -> int:
        with self._lock:
            return len(self._order)
