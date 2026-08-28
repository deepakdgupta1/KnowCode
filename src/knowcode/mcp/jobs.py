"""Background job registry for long-running MCP lifecycle actions.

Indexing costs one embedding round-trip per file with no batching across
files and no concurrency, so a cold build on a few thousand files can run
for tens of minutes. Claude Code's stdio idle timeout defaults to 30
minutes, which makes a synchronous ``build`` tool call two problems at once:
the agent's turn freezes with no feedback, and a large repository can have
its build killed mid-flight.

So lifecycle actions submit a job and return its id immediately; the agent
polls ``job_status`` and can report progress to the user while indexing runs.
One job runs at a time — ``build_generation`` cleans up staging directories
on entry, so two concurrent builds would clobber each other's staging.

Records are frozen and replaced rather than mutated, so a snapshot handed to
a caller never changes underneath it.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Optional

from knowcode.errors import KnowCodePrerequisiteError

#: A job body receives a progress reporter and returns its result payload.
ProgressReporter = Callable[[int, Optional[int]], None]
JobBody = Callable[[ProgressReporter], dict[str, Any]]

#: How a job body is run. The default spawns a daemon thread; tests inject a
#: synchronous executor so lifecycle assertions stay deterministic.
Executor = Callable[[Callable[[], None]], None]

DEFAULT_MAX_HISTORY = 20

logger = logging.getLogger(__name__)


class JobState(str, Enum):
    """Lifecycle of a single background job."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self is not JobState.RUNNING


class JobConflictError(KnowCodePrerequisiteError):
    """Raised when a lifecycle job is submitted while another is running."""

    def __init__(self, running_job_id: str, running_action: str) -> None:
        super().__init__(
            f"A {running_action} job is already running: {running_job_id}",
            code="job_already_running",
            hint=(
                f"Poll job_status with job_id={running_job_id!r} until it "
                "reaches a terminal state before starting another build."
            ),
        )
        self.running_job_id = running_job_id
        self.running_action = running_action


@dataclass(frozen=True)
class Job:
    """An immutable snapshot of one lifecycle job."""

    job_id: str
    action: str
    state: JobState
    started_at: float
    finished_at: Optional[float] = None
    files_done: int = 0
    files_total: Optional[int] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    code: Optional[str] = None
    hint: Optional[str] = None

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        """Project to the MCP response shape, omitting empty fields.

        Empty fields are dropped rather than sent as nulls: this payload is
        polled repeatedly, and the token budget is the whole point of the
        product.
        """
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "action": self.action,
            "state": self.state.value,
            "elapsed_s": self.elapsed_s,
        }
        if self.files_total is not None:
            payload["files_done"] = self.files_done
            payload["files_total"] = self.files_total
            if self.state is JobState.RUNNING and self.files_done > 0:
                per_file = self.elapsed_s / self.files_done
                remaining = max(self.files_total - self.files_done, 0)
                payload["eta_s"] = round(per_file * remaining, 1)
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        if self.code is not None:
            payload["code"] = self.code
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload


def _thread_executor(fn: Callable[[], None]) -> None:
    """Run the job body on a daemon thread so submit returns immediately."""
    threading.Thread(target=fn, daemon=True, name="knowcode-mcp-job").start()


def _default_id_factory() -> str:
    return f"j-{secrets.token_hex(4)}"


class JobRegistry:
    """Tracks lifecycle jobs for one MCP server process.

    State is in-process: the registry lives as long as the server, which is
    the same lifetime as the agent session that spawned it. A job cannot
    outlive the process that owns it, so there is nothing to recover.
    """

    def __init__(
        self,
        executor: Optional[Executor] = None,
        id_factory: Optional[Callable[[], str]] = None,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        self._executor: Executor = executor or _thread_executor
        self._id_factory = id_factory or _default_id_factory
        self._max_history = max(1, max_history)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._done = threading.Condition(self._lock)

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(
        self,
        action: str,
        body: JobBody,
        on_done: Optional[Callable[[], None]] = None,
    ) -> Job:
        """Register and start a job, returning its initial snapshot.

        Args:
            action: Lifecycle action name, for reporting.
            body: Callable receiving a progress reporter, returning a result
                payload.
            on_done: Called once the job reaches a terminal state, whatever the
                outcome. Used to refresh caches the job invalidated. Runs on the
                job thread; its failure is logged, never propagated.

        Returns:
            The job as it was at submission time (state ``RUNNING``).

        Raises:
            JobConflictError: If another job is still running.
        """
        with self._lock:
            running = self._running_locked()
            if running is not None:
                raise JobConflictError(running.job_id, running.action)

            job_id = self._id_factory()
            job = Job(
                job_id=job_id,
                action=action,
                state=JobState.RUNNING,
                started_at=time.monotonic(),
            )
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._evict_locked()

        self._executor(lambda: self._run(job_id, body, on_done))
        return job

    def _run(
        self,
        job_id: str,
        body: JobBody,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Execute one job body and record its terminal state.

        The job record keeps only a message, and it is in-memory, capped, and
        lost on restart — so every failure is also logged with its traceback.
        Without that, a mutating job that already spent embedding quota could
        fail with a bare string and leave nothing to diagnose: this runs on a
        daemon thread inside ``try``, so ``threading.excepthook`` never fires.
        """

        def report(done: int, total: Optional[int]) -> None:
            self._update(job_id, files_done=done, files_total=total)

        try:
            result = body(report)
        except KnowCodePrerequisiteError as exc:
            logger.warning("Job %s missing a prerequisite: %s", job_id, exc)
            self._finish(
                job_id,
                JobState.FAILED,
                error=str(exc),
                code=exc.code,
                hint=exc.hint,
            )
        except Exception as exc:  # noqa: BLE001 - recorded and logged, never swallowed
            logger.exception("Job %s failed: %s", job_id, exc)
            self._finish(job_id, JobState.FAILED, error=str(exc) or type(exc).__name__)
        else:
            self._finish(job_id, JobState.SUCCEEDED, result=result)
        finally:
            if on_done is not None:
                try:
                    on_done()
                except Exception as exc:  # noqa: BLE001 - never mask the outcome
                    logger.warning("Job %s completion hook failed: %s", job_id, exc)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> Optional[Job]:
        """Return the current snapshot of a job, or ``None`` if unknown."""
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Optional[Job]:
        """Return the most recently submitted job, if any."""
        with self._lock:
            if not self._order:
                return None
            return self._jobs.get(self._order[-1])

    def running(self) -> Optional[Job]:
        """Return the in-flight job, if one exists."""
        with self._lock:
            return self._running_locked()

    def wait(self, job_id: str, timeout: float) -> bool:
        """Block until a job is terminal. Returns ``False`` on timeout.

        Used by tests and by ``doctor``; the MCP surface itself never blocks.
        """
        deadline = time.monotonic() + timeout
        with self._done:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return False
                if job.state.is_terminal:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._done.wait(remaining)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _running_locked(self) -> Optional[Job]:
        for job_id in reversed(self._order):
            job = self._jobs.get(job_id)
            if job is not None and job.state is JobState.RUNNING:
                return job
        return None

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is None or existing.state.is_terminal:
                return
            self._jobs[job_id] = replace(existing, **changes)

    def _finish(self, job_id: str, state: JobState, **changes: Any) -> None:
        with self._done:
            existing = self._jobs.get(job_id)
            if existing is None:
                return
            self._jobs[job_id] = replace(
                existing, state=state, finished_at=time.monotonic(), **changes
            )
            self._done.notify_all()

    def _evict_locked(self) -> None:
        while len(self._order) > self._max_history:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)
