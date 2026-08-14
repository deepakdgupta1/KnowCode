"""Coalescing and ordering semantics of the watch queue (Step 16).

Every test here states what committing the queue's contents would do to the
index, because that — not the number of events observed — is the invariant: a
burst of filesystem events must converge on the index the *final* filesystem
state implies.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from knowcode.indexing.watch_queue import (
    WatchOp,
    WatchQueue,
    WatchQueueClosed,
    WatchWork,
)
from knowcode.utils.entity_identity import normalize_file_identity


@pytest.fixture
def queue() -> WatchQueue:
    return WatchQueue()


def _drain(queue: WatchQueue) -> list[WatchWork]:
    """Take every item the way the worker would, in order."""
    taken: list[WatchWork] = []
    while True:
        work = queue.take(timeout=0.01)
        if work is None:
            return taken
        taken.append(work)
        queue.complete(work)


# ----------------------------------------------------------------------
# Coalescing: one item per canonical identity, last event wins
# ----------------------------------------------------------------------


def test_a_burst_of_modifies_collapses_into_one_item(queue: WatchQueue, tmp_path: Path) -> None:
    """The reviewed defect: five modify events committed five transactions."""
    source = tmp_path / "m.py"
    for _ in range(5):
        queue.submit(WatchWork.index(source))

    taken = _drain(queue)

    assert [(work.op, work.path) for work in taken] == [
        (WatchOp.INDEX, normalize_file_identity(source))
    ]


def test_path_aliases_are_one_item(queue: WatchQueue, tmp_path: Path) -> None:
    """The reviewed defect: two spellings of one file were two work items."""
    source = tmp_path / "m.py"
    (tmp_path / "sub").mkdir()

    queue.submit(WatchWork.index(source))
    queue.submit(WatchWork.index(tmp_path / "sub" / ".." / "m.py"))
    queue.submit(WatchWork.index(str(source)))

    assert len(_drain(queue)) == 1


def test_a_delete_after_modifies_commits_the_delete(queue: WatchQueue, tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    queue.submit(WatchWork.index(source))
    queue.submit(WatchWork.index(source))
    queue.submit(WatchWork.delete(source))

    taken = _drain(queue)

    assert [(work.op, work.path) for work in taken] == [
        (WatchOp.DELETE, normalize_file_identity(source))
    ]


def test_a_create_after_a_delete_commits_the_index(queue: WatchQueue, tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    queue.submit(WatchWork.delete(source))
    queue.submit(WatchWork.index(source))

    taken = _drain(queue)

    assert [work.op for work in taken] == [WatchOp.INDEX]


def test_coalescing_keeps_the_original_queue_position(queue: WatchQueue, tmp_path: Path) -> None:
    """A file saved in a loop must not starve the files queued behind it."""
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(first))
    queue.submit(WatchWork.index(second))
    queue.submit(WatchWork.index(first))

    assert [work.path for work in _drain(queue)] == [
        normalize_file_identity(first),
        normalize_file_identity(second),
    ]


def test_an_event_during_an_in_flight_commit_schedules_a_new_item(
    queue: WatchQueue, tmp_path: Path
) -> None:
    """An in-flight commit already read the file, so it cannot absorb an edit."""
    source = tmp_path / "m.py"
    queue.submit(WatchWork.index(source))

    in_flight = queue.take(timeout=0.01)
    assert in_flight is not None
    queue.submit(WatchWork.index(source))  # the developer saved again
    queue.complete(in_flight)

    assert [work.path for work in _drain(queue)] == [normalize_file_identity(source)]


# ----------------------------------------------------------------------
# Moves: the destination is proven before any source is dropped
# ----------------------------------------------------------------------


def test_a_move_is_one_item_that_drops_its_source(queue: WatchQueue, tmp_path: Path) -> None:
    old, new = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(new, moved_from=old))

    (work,) = _drain(queue)

    assert work.op is WatchOp.INDEX
    assert work.path == normalize_file_identity(new)
    assert work.dropped_paths == (normalize_file_identity(old),)


def test_a_move_chain_collapses_into_one_commit(queue: WatchQueue, tmp_path: Path) -> None:
    """`a -> b` then `b -> c` indexes only `c`, and drops both `a` and `b`."""
    a, b, c = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"
    queue.submit(WatchWork.index(b, moved_from=a))
    queue.submit(WatchWork.index(c, moved_from=b))

    (work,) = _drain(queue)

    assert work.path == normalize_file_identity(c)
    assert set(work.dropped_paths) == {
        normalize_file_identity(a),
        normalize_file_identity(b),
    }


def test_a_move_back_to_the_original_name_drops_nothing_extra(
    queue: WatchQueue, tmp_path: Path
) -> None:
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(b, moved_from=a))
    queue.submit(WatchWork.index(a, moved_from=b))

    (work,) = _drain(queue)

    assert work.path == normalize_file_identity(a)
    assert work.dropped_paths == (normalize_file_identity(b),)


def test_a_self_move_drops_nothing(queue: WatchQueue, tmp_path: Path) -> None:
    """Endpoints that normalize to one identity are a replacement, not a delete."""
    source = tmp_path / "m.py"
    queue.submit(WatchWork.index(source, moved_from=tmp_path / "sub" / ".." / "m.py"))

    (work,) = _drain(queue)

    assert work.dropped_paths == ()


def test_a_modify_after_a_move_still_drops_the_old_identity(
    queue: WatchQueue, tmp_path: Path
) -> None:
    """Otherwise the pre-rename identity stays in the index forever."""
    old, new = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(new, moved_from=old))
    queue.submit(WatchWork.index(new))

    (work,) = _drain(queue)

    assert work.dropped_paths == (normalize_file_identity(old),)


def test_a_delete_of_a_move_destination_still_drops_the_source(
    queue: WatchQueue, tmp_path: Path
) -> None:
    old, new = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(new, moved_from=old))
    queue.submit(WatchWork.delete(new))

    (work,) = _drain(queue)

    assert work.op is WatchOp.DELETE
    assert work.dropped_paths == (normalize_file_identity(old),)


def test_recreating_a_moved_from_path_cancels_its_drop(
    queue: WatchQueue, tmp_path: Path
) -> None:
    """`git mv a b` then a new `a`: the new `a` must survive whatever order wins."""
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    queue.submit(WatchWork.index(b, moved_from=a))
    queue.submit(WatchWork.index(a))

    taken = _drain(queue)

    assert [work.path for work in taken] == [
        normalize_file_identity(b),
        normalize_file_identity(a),
    ]
    assert taken[0].dropped_paths == ()


# ----------------------------------------------------------------------
# Retries
# ----------------------------------------------------------------------


def test_a_retry_returns_the_item_with_the_next_attempt(
    queue: WatchQueue, tmp_path: Path
) -> None:
    queue.submit(WatchWork.index(tmp_path / "m.py"))
    work = queue.take(timeout=0.01)
    assert work is not None

    assert queue.requeue(work) is True
    queue.complete(work)

    (retried,) = _drain(queue)
    assert retried.attempt == 2


def test_a_newer_event_supersedes_a_retry(queue: WatchQueue, tmp_path: Path) -> None:
    """The new event re-reads the file, so replaying the failed one is waste."""
    source = tmp_path / "m.py"
    queue.submit(WatchWork.index(source))
    work = queue.take(timeout=0.01)
    assert work is not None
    queue.submit(WatchWork.index(source))

    assert queue.requeue(work) is False
    queue.complete(work)

    (pending,) = _drain(queue)
    assert pending.attempt == 1


def test_a_closed_queue_still_accepts_retries(queue: WatchQueue, tmp_path: Path) -> None:
    """Abandoning an in-progress transaction at shutdown is the loss to avoid."""
    queue.submit(WatchWork.index(tmp_path / "m.py"))
    work = queue.take(timeout=0.01)
    assert work is not None
    queue.close()

    assert queue.requeue(work) is True


# ----------------------------------------------------------------------
# Lifecycle and drain accounting
# ----------------------------------------------------------------------


def test_a_closed_queue_rejects_new_work(queue: WatchQueue, tmp_path: Path) -> None:
    queue.close()

    with pytest.raises(WatchQueueClosed):
        queue.submit(WatchWork.index(tmp_path / "m.py"))


def test_join_waits_for_the_in_flight_item_not_just_the_backlog(
    queue: WatchQueue, tmp_path: Path
) -> None:
    """A drain that ignored in-flight work would report durability it lacks."""
    queue.submit(WatchWork.index(tmp_path / "m.py"))
    work = queue.take(timeout=0.01)
    assert work is not None

    assert queue.join(timeout=0.05) is False

    queue.complete(work)
    assert queue.join(timeout=1.0) is True


def test_join_returns_when_a_producer_is_still_ahead_of_the_consumer(
    queue: WatchQueue, tmp_path: Path
) -> None:
    """Barrier-driven: the consumer takes the item only after join() is waiting."""
    queue.submit(WatchWork.index(tmp_path / "m.py"))
    at_barrier = threading.Barrier(2, timeout=5.0)

    def consume() -> None:
        at_barrier.wait()
        work = queue.take(timeout=1.0)
        assert work is not None
        queue.complete(work)

    consumer = threading.Thread(target=consume)
    consumer.start()
    at_barrier.wait()
    try:
        assert queue.join(timeout=5.0) is True
    finally:
        consumer.join(timeout=5.0)

    assert queue.pending() == ()
    assert queue.in_flight is None


def test_pending_reports_uncommitted_work_in_order(queue: WatchQueue, tmp_path: Path) -> None:
    queue.submit(WatchWork.index(tmp_path / "a.py"))
    queue.submit(WatchWork.delete(tmp_path / "b.py"))

    assert [work.path for work in queue.pending()] == [
        normalize_file_identity(tmp_path / "a.py"),
        normalize_file_identity(tmp_path / "b.py"),
    ]
    assert len(queue) == 2
