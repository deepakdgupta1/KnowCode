"""Local, aggregate-only telemetry for KnowCode (ADR 5).

Every event goes through one path: an allowlist
(:mod:`knowcode.telemetry_policy`), recursive redaction
(:mod:`knowcode.telemetry_redaction`), and a bounded writer
(:mod:`knowcode.telemetry_files`). Callers cannot bypass it, which is the point
— the previous sink wrote whatever dict it was handed, so a raw query, an MCP
client's whole argument payload, and any credential inside either went to disk.

The unit of counting is a **query scope**. One logical question opens one
scope, no matter how many retrieval attempts it makes or how many layers it
passes through, and exactly one counted ``query`` event is written when it
closes. Everything else — a routing decision, an MCP tool call, a reranker
latency — is a separate event type that describes an already-counted query.

Nothing here is transmitted anywhere. Telemetry is a local file the user owns
and can delete with :func:`delete_telemetry`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from knowcode import telemetry_files, telemetry_policy, telemetry_redaction
from knowcode.telemetry_files import (  # re-exported for callers and tests
    correlation_key_path,
    delete_telemetry_files,
    raw_telemetry_path,
    telemetry_path,
)

logger = logging.getLogger(__name__)

#: Environment flag that turns on raw-query capture. Off by default: ADR 5
#: treats redaction as defense in depth, not as a safe default for raw text.
RAW_CAPTURE_ENV = "KNOWCODE_TELEMETRY_RAW"

#: Upper bound on writes queued behind a slow or unwritable disk. Telemetry
#: drops rather than growing without bound; it must never apply backpressure
#: to the query path.
MAX_PENDING_EVENTS = 512

#: The writer pool is created on first use and released by
#: :func:`shutdown_telemetry`, so a server shutdown (Step 17) can drain queued
#: writes instead of letting the process exit with them still in the pool. It is
#: re-created on demand, so one server's shutdown never breaks the next one's
#: logging in the same process.
_executor: Optional[ThreadPoolExecutor] = None
_pending: list["Future[None]"] = []
_executor_lock = threading.Lock()

_dropped_events = 0
_dropped_lock = threading.Lock()
_raw_warned = False

_active_scope: ContextVar[Optional["QueryScope"]] = ContextVar(
    "knowcode_query_scope", default=None
)


# ----------------------------------------------------------------------
# Writer pool
# ----------------------------------------------------------------------


def _count_drop() -> None:
    global _dropped_events
    with _dropped_lock:
        _dropped_events += 1


def dropped_event_count() -> int:
    """Events discarded by policy, backpressure, or a failed write.

    Exposed so a test — or an operator wondering why a metric is missing — can
    tell "nothing happened" from "telemetry refused to record it".
    """
    with _dropped_lock:
        return _dropped_events


def _submit(function: Callable[..., None], *args: Any) -> None:
    """Queue one write on the pool, creating it if needed."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="knowcode-telemetry"
            )
        _pending[:] = [future for future in _pending if not future.done()]
        if len(_pending) >= MAX_PENDING_EVENTS:
            _count_drop()
            return
        _pending.append(_executor.submit(function, *args))


def shutdown_telemetry(timeout: float = 5.0) -> bool:
    """Drain queued telemetry writes and release the pool. Idempotent.

    Args:
        timeout: Upper bound on the drain, so shutdown stays bounded even when
            the telemetry directory has become unwritable.

    Returns:
        Whether every accepted write finished. ``False`` means some were still
        running when the budget ran out — the caller reports that rather than
        claiming telemetry is durable.
    """
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
        pending, _pending[:] = list(_pending), []
    if executor is None:
        return True
    _, not_done = wait(pending, timeout=timeout)
    executor.shutdown(wait=False)
    return not not_done


def _dispatch(function: Callable[..., None], *args: Any) -> None:
    """Run a write now under test, otherwise on the pool."""
    if os.environ.get("KNOWCODE_TESTING") == "1":
        function(*args)
    else:
        _submit(function, *args)


# ----------------------------------------------------------------------
# Public logging entry point
# ----------------------------------------------------------------------


def log_event(store_path: str | Path | None, event: Mapping[str, Any]) -> None:
    """Record one event, subject to the schema allowlist and redaction.

    Args:
        store_path: Any path identifying the store. ``None`` falls back to the
            active query scope, which is how callers with no store handle of
            their own (the reranker) stay inside the store root.
        event: Caller fields. Anything outside the allowlist for its
            ``event_type`` is dropped; an unreviewed ``event_type`` is
            rejected entirely.
    """
    scope = _active_scope.get()
    root = telemetry_files.resolve_store_root(store_path)
    if root is None and scope is not None:
        root = scope.store_root
    if root is None:
        _count_drop()
        return

    record = telemetry_policy.sanitize_event(event)
    if record is None:
        _count_drop()
        return

    _dispatch(_write_event_sync, root, telemetry_redaction.redact(record))


def _write_event_sync(store_path: str | Path, event: Mapping[str, Any]) -> None:
    """Serialize one sanitized event and append it under the file bounds."""
    try:
        line = json.dumps(event, ensure_ascii=False) + "\n"
    except (TypeError, ValueError):
        _count_drop()
        return
    if len(line) > telemetry_files.MAX_RECORD_CHARS:
        _count_drop()
        return
    try:
        telemetry_files.append_record(telemetry_files.telemetry_path(store_path), line)
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a query
        _count_drop()
        logger.warning("Telemetry log failed: %s", exc)


# ----------------------------------------------------------------------
# Opt-in raw capture
# ----------------------------------------------------------------------


def raw_capture_enabled() -> bool:
    """Whether the operator has explicitly opted into raw-query capture."""
    return os.environ.get(RAW_CAPTURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def reset_raw_capture_warning() -> None:
    """Re-arm the one-per-process opt-in warning (used by tests)."""
    global _raw_warned
    _raw_warned = False


def _write_raw_sync(store_path: str | Path, event: Mapping[str, Any]) -> None:
    """Append one raw-capture record to its own short-retention file."""
    try:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        telemetry_files.append_record(
            telemetry_files.raw_telemetry_path(store_path), line, raw=True
        )
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a query
        _count_drop()
        logger.warning("Raw telemetry log failed: %s", exc)


def _capture_raw_query(root: Path, query_id: str, query: str) -> None:
    """Write the raw question, still redacted, to the opt-in file."""
    global _raw_warned
    if not _raw_warned:
        _raw_warned = True
        logger.warning(
            "%s is set: raw query text is being written to %s. This file is "
            "excluded from the default schema, kept for %s days, and removed by "
            "'knowcode telemetry clear'.",
            RAW_CAPTURE_ENV,
            telemetry_files.RAW_TELEMETRY_FILENAME,
            telemetry_files.RAW_RETENTION_DAYS,
        )
    record = {
        "telemetry_schema_version": telemetry_policy.TELEMETRY_SCHEMA_VERSION,
        "event_type": "raw_query",
        "timestamp": int(time.time()),
        "query_id": query_id,
        "query": telemetry_redaction.redact_text(query),
    }
    _dispatch(_write_raw_sync, root, record)


# ----------------------------------------------------------------------
# Query scope: one logical query, one counted event
# ----------------------------------------------------------------------


class QueryScope:
    """Accumulates one logical query's metadata; emits one event on close.

    A scope is entered by whichever layer starts the query — the agent, the
    service, the MCP server, or the retrieval orchestrator — and re-entering it
    from a deeper layer yields the *same* object. That is what makes "one
    logical query, one counted event" true regardless of how many retrieval
    attempts the agent makes or which entry point the user came through.
    """

    __slots__ = (
        "store_root",
        "query_id",
        "query_chars",
        "entry_point",
        "_fields",
        "_retrievals",
        "_started",
        "_lock",
    )

    def __init__(self, store_root: Path, query: str, entry_point: str) -> None:
        self.store_root = store_root
        self.query_id = telemetry_files.query_id(store_root, query)
        self.query_chars = len(query)
        self.entry_point = entry_point
        self._fields: dict[str, Any] = {}
        self._retrievals = 0
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def annotate(self, **fields: Any) -> None:
        """Merge terminal metadata (routing outcome, staleness, verbosity)."""
        with self._lock:
            self._fields.update(fields)

    def record_retrieval(self, **fields: Any) -> None:
        """Merge the outcome of one retrieval attempt and count it."""
        with self._lock:
            self._retrievals += 1
            self._fields.update(fields)

    def _event(self, outcome: str) -> dict[str, Any]:
        with self._lock:
            fields = dict(self._fields)
            retrievals = self._retrievals
        event: dict[str, Any] = {
            "event_type": telemetry_policy.QUERY_EVENT,
            "query_id": self.query_id,
            "query_chars": self.query_chars,
            "query_length_bucket": telemetry_policy.length_bucket(self.query_chars),
            "entry_point": self.entry_point,
            "retrievals": retrievals,
            "duration_ms": max(0, int((time.monotonic() - self._started) * 1000)),
            "outcome": outcome,
        }
        event.update(fields)
        score = event.get("sufficiency_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            event["sufficiency_bucket"] = telemetry_policy.score_bucket(float(score))
        return event


@contextmanager
def query_scope(
    store_path: str | Path | None,
    *,
    query: str,
    entry_point: str = "service",
) -> Iterator[QueryScope]:
    """Open (or join) the scope for one logical query.

    Yields the active scope if one already exists, so nested layers contribute
    to a single counted event. The outermost scope writes that event when it
    closes — including when the query raised, because a failing query is the
    one an operator most wants to see in the trend.
    """
    existing = _active_scope.get()
    if existing is not None:
        yield existing
        return

    root = telemetry_files.resolve_store_root(store_path)
    if root is None:
        _count_drop()
        yield QueryScope(Path("."), query, entry_point)
        return

    scope = QueryScope(root, query, entry_point)
    token = _active_scope.set(scope)
    outcome = "ok"
    try:
        yield scope
    except BaseException:
        outcome = "error"
        raise
    finally:
        _active_scope.reset(token)
        try:
            if raw_capture_enabled():
                _capture_raw_query(root, scope.query_id, query)
            log_event(root, scope._event(outcome))
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a query
            _count_drop()
            logger.warning("Telemetry query event failed: %s", exc)


def current_query_scope() -> QueryScope | None:
    """The scope for the query being served on this thread, if any."""
    return _active_scope.get()


def current_query_id() -> str:
    """Correlation id of the query being served, or ``""`` outside a scope."""
    scope = _active_scope.get()
    return scope.query_id if scope is not None else ""


# ----------------------------------------------------------------------
# Reading and deleting
# ----------------------------------------------------------------------


def get_telemetry_summary(store_path: str | Path) -> dict[str, Any]:
    """Aggregate metrics for trend review, across rotations and schemas."""
    empty: dict[str, Any] = {
        "total_queries": 0,
        "local_routing_rate": 0.0,
        "average_sufficiency_score": 0.0,
        "user_marked_misses": 0,
        "schema_version": telemetry_policy.TELEMETRY_SCHEMA_VERSION,
        "events_by_type": {},
        "raw_capture_enabled": raw_capture_enabled(),
    }
    root = telemetry_files.resolve_store_root(store_path)
    if root is None:
        return empty

    queries = 0
    local = 0
    sufficiency = 0.0
    misses = 0
    by_type: dict[str, int] = {}
    try:
        for record in telemetry_files.iter_records(telemetry_files.telemetry_path(root)):
            event_type = str(record.get("event_type", "unknown"))
            by_type[event_type] = by_type.get(event_type, 0) + 1
            if telemetry_policy.is_counted_query_event(record):
                queries += 1
                if "local" in (record.get("local_or_escalated"), record.get("source")):
                    local += 1
                score = record.get("sufficiency_score", 0.0)
                if isinstance(score, (int, float)) and not isinstance(score, bool):
                    sufficiency += float(score)
            if record.get("user_marked_miss") or record.get("user_marked_misses"):
                misses += 1
    except OSError as exc:
        logger.warning("Telemetry summary failed: %s", exc)
        return empty

    return {
        "total_queries": queries,
        "local_routing_rate": (local / queries) if queries else 0.0,
        "average_sufficiency_score": (sufficiency / queries) if queries else 0.0,
        "user_marked_misses": misses,
        "schema_version": telemetry_policy.TELEMETRY_SCHEMA_VERSION,
        "events_by_type": by_type,
        "raw_capture_enabled": raw_capture_enabled(),
    }


def delete_telemetry(store_path: str | Path) -> dict[str, Any]:
    """Remove every telemetry artifact for a store, including its key.

    Deleting the correlation key means the next query starts a fresh
    identifier space, so deletion is not merely a truncation of history.
    """
    return delete_telemetry_files(store_path)


__all__ = [
    "MAX_PENDING_EVENTS",
    "QueryScope",
    "RAW_CAPTURE_ENV",
    "correlation_key_path",
    "current_query_id",
    "current_query_scope",
    "delete_telemetry",
    "dropped_event_count",
    "get_telemetry_summary",
    "log_event",
    "query_scope",
    "raw_capture_enabled",
    "raw_telemetry_path",
    "reset_raw_capture_warning",
    "shutdown_telemetry",
    "telemetry_path",
]
