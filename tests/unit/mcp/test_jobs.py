"""Unit tests for the MCP lifecycle job registry.

A cold ``build`` indexes one file per embedding round-trip with no
concurrency, so a few thousand files can run for tens of minutes. Claude
Code's stdio idle timeout defaults to 30 minutes, so a synchronous build
would both freeze the agent's turn and risk being killed mid-flight.
Lifecycle actions therefore return a job id immediately and the agent polls.

Tests inject a synchronous executor so job lifecycle is deterministic; the
threading default is exercised separately.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from knowcode.mcp.jobs import (
    JobConflictError,
    JobRegistry,
    JobState,
)


def _inline(fn: Any) -> None:
    """Executor that runs the job body synchronously, before submit returns."""
    fn()


def _ids(*values: str) -> Any:
    """Deterministic id factory for assertions on job ids."""
    remaining = list(values)

    def factory() -> str:
        return remaining.pop(0)

    return factory


class TestSubmit:
    def test_successful_job_reaches_succeeded_with_result(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        job = registry.submit("build", lambda _progress: {"published": True})

        assert job.job_id == "j-1"
        assert job.action == "build"
        final = registry.get("j-1")
        assert final is not None
        assert final.state == JobState.SUCCEEDED
        assert final.result == {"published": True}
        assert final.error is None
        assert final.finished_at is not None

    def test_failing_job_reaches_failed_and_captures_the_error(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        def boom(_progress: Any) -> dict[str, Any]:
            raise RuntimeError("parse exploded")

        registry.submit("build", boom)

        final = registry.get("j-1")
        assert final is not None
        assert final.state == JobState.FAILED
        assert final.error is not None
        assert "parse exploded" in final.error
        assert final.result is None

    def test_prerequisite_error_preserves_code_and_hint(self) -> None:
        """A typed KnowCode error keeps its actionable fields for the agent."""
        from knowcode.errors import MissingKnowledgeStoreError

        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        def missing(_progress: Any) -> dict[str, Any]:
            raise MissingKnowledgeStoreError(  # noqa: TRY301
                __import__("pathlib").Path("/x/knowcode_knowledge.json")
            )

        registry.submit("build", missing)

        final = registry.get("j-1")
        assert final is not None
        assert final.code == "missing_knowledge_store"
        assert final.hint is not None
        assert "knowcode build" in final.hint

    def test_job_body_receives_a_progress_reporter(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        def body(progress: Any) -> dict[str, Any]:
            progress(3, 10)
            return {"ok": True}

        registry.submit("build", body)

        final = registry.get("j-1")
        assert final is not None
        assert final.files_done == 3
        assert final.files_total == 10

    def test_progress_survives_into_a_terminal_state(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        def body(progress: Any) -> dict[str, Any]:
            progress(10, 10)
            return {"ok": True}

        registry.submit("build", body)

        final = registry.get("j-1")
        assert final is not None
        assert final.state == JobState.SUCCEEDED
        assert final.files_done == 10


class TestSerialization:
    """Two concurrent builds would race on the same staging directory."""

    def test_second_submit_while_one_runs_is_rejected(self) -> None:
        registry = JobRegistry(
            executor=lambda fn: None,  # never runs: job stays RUNNING
            id_factory=_ids("j-1", "j-2"),
        )
        first = registry.submit("build", lambda _p: {})

        with pytest.raises(JobConflictError) as excinfo:
            registry.submit("index", lambda _p: {})

        assert first.job_id in str(excinfo.value)
        assert excinfo.value.code == "job_already_running"

    def test_submit_is_allowed_once_the_previous_job_is_terminal(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1", "j-2"))
        registry.submit("build", lambda _p: {})

        second = registry.submit("index", lambda _p: {})

        assert second.job_id == "j-2"

    def test_a_failed_job_does_not_block_the_next_submit(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1", "j-2"))

        def boom(_p: Any) -> dict[str, Any]:
            raise RuntimeError("nope")

        registry.submit("build", boom)

        assert registry.submit("build", lambda _p: {}).job_id == "j-2"


class TestLookup:
    def test_unknown_job_id_returns_none(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        assert registry.get("does-not-exist") is None

    def test_latest_returns_the_most_recent_job(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1", "j-2"))
        registry.submit("build", lambda _p: {})
        registry.submit("index", lambda _p: {})

        latest = registry.latest()
        assert latest is not None
        assert latest.job_id == "j-2"

    def test_latest_is_none_on_a_fresh_registry(self) -> None:
        assert JobRegistry(executor=_inline).latest() is None

    def test_history_is_capped_and_evicts_oldest_first(self) -> None:
        registry = JobRegistry(
            executor=_inline,
            id_factory=_ids("j-1", "j-2", "j-3"),
            max_history=2,
        )
        registry.submit("build", lambda _p: {})
        registry.submit("build", lambda _p: {})
        registry.submit("build", lambda _p: {})

        assert registry.get("j-1") is None
        assert registry.get("j-3") is not None


class TestImmutability:
    """Job records are replaced, never mutated in place."""

    def test_job_is_frozen(self) -> None:
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))
        job = registry.submit("build", lambda _p: {})

        with pytest.raises(Exception):
            job.state = JobState.FAILED  # type: ignore[misc]

    def test_handle_returned_by_submit_is_a_snapshot(self) -> None:
        """The caller's handle does not mutate as the job progresses."""
        registry = JobRegistry(executor=_inline, id_factory=_ids("j-1"))

        handle = registry.submit("build", lambda _p: {"published": True})

        assert handle.state == JobState.RUNNING
        stored = registry.get("j-1")
        assert stored is not None
        assert stored.state == JobState.SUCCEEDED


class TestThreadedExecutor:
    """The production path really does run off the calling thread."""

    def test_default_executor_does_not_block_submit(self) -> None:
        registry = JobRegistry(id_factory=_ids("j-1"))
        release = threading.Event()
        entered = threading.Event()

        def body(_progress: Any) -> dict[str, Any]:
            entered.set()
            release.wait(timeout=5)
            return {"published": True}

        handle = registry.submit("build", body)

        assert entered.wait(timeout=5), "job body never started"
        assert handle.state == JobState.RUNNING
        running = registry.get("j-1")
        assert running is not None
        assert running.state == JobState.RUNNING

        release.set()
        assert registry.wait("j-1", timeout=5)
        done = registry.get("j-1")
        assert done is not None
        assert done.state == JobState.SUCCEEDED

    def test_wait_reports_false_on_timeout(self) -> None:
        registry = JobRegistry(executor=lambda fn: None, id_factory=_ids("j-1"))
        registry.submit("build", lambda _p: {})

        assert registry.wait("j-1", timeout=0.05) is False
