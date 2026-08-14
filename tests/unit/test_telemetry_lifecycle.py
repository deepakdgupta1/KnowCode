"""The telemetry executor is an owned resource with a shutdown (Step 17).

``telemetry._executor`` was a module-level ``ThreadPoolExecutor`` created at
import and never shut down. A queued write could therefore still be in the pool
when the process exited, and no caller could ask whether telemetry was durable.
Step 17 gives it the same contract as every other owned resource: an explicit,
bounded, idempotent shutdown that does not break the next server in the process.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from knowcode import telemetry, telemetry_files

TIMEOUT = 5.0


def _event(method: str) -> dict[str, object]:
    """A policy-valid event (Step 20); the pool never sees raw text now."""
    return {"event_type": "reranker_latency", "method": method, "num_chunks": 1}


@pytest.fixture(autouse=True)
def _restore_executor():  # type: ignore[no-untyped-def]
    """The pool is process state; leave it usable for the rest of the suite."""
    yield
    telemetry.shutdown_telemetry(timeout=TIMEOUT)


def test_shutdown_drains_a_queued_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write accepted before shutdown must be on disk after it."""
    monkeypatch.delenv("KNOWCODE_TESTING", raising=False)
    telemetry.shutdown_telemetry(timeout=TIMEOUT)

    released = threading.Event()
    original = telemetry._write_event_sync

    def slow_write(store_path, event):  # type: ignore[no-untyped-def]
        assert released.wait(TIMEOUT), "the write never got its release"
        original(store_path, event)

    monkeypatch.setattr(telemetry, "_write_event_sync", slow_write)
    telemetry.log_event(tmp_path, _event("queued"))
    released.set()

    assert telemetry.shutdown_telemetry(timeout=TIMEOUT)

    log_file = telemetry_files.telemetry_path(tmp_path)
    assert log_file.exists()
    assert json.loads(log_file.read_text(encoding="utf-8").strip())["method"] == "queued"


def test_shutdown_is_idempotent() -> None:
    """Shutting down twice is a no-op, not an error."""
    assert telemetry.shutdown_telemetry(timeout=TIMEOUT)
    assert telemetry.shutdown_telemetry(timeout=TIMEOUT)


def test_logging_after_shutdown_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second server in the same process must not inherit a dead pool.

    Without this, one API shutdown would make every later ``log_event`` raise
    ``RuntimeError: cannot schedule new futures after shutdown``.
    """
    monkeypatch.delenv("KNOWCODE_TESTING", raising=False)
    telemetry.shutdown_telemetry(timeout=TIMEOUT)

    telemetry.log_event(tmp_path, _event("after"))
    assert telemetry.shutdown_telemetry(timeout=TIMEOUT)

    log_file = telemetry_files.telemetry_path(tmp_path)
    assert json.loads(log_file.read_text(encoding="utf-8").strip())["method"] == "after"
