"""Server resource ownership and bounded teardown (Step 17).

``create_app`` used to start a :class:`~knowcode.indexing.background_indexer.
BackgroundIndexer` and a :class:`~knowcode.indexing.monitor.FileMonitor` and
stash them on ``app.state`` "to keep alive". Nothing ever stopped them. Running
a complete ASGI lifespan over the pre-Step-17 app left the worker thread
running, the observer alive, the SQLite chunk repository open, and the global
service installed — and work queued at shutdown was simply lost with the daemon
thread, with nothing reporting it.

This module gives the server one owner:

* **One creator, one closer.** Whatever :class:`ServerResources` builds, it
  releases. The service closes what *it* opened (stores, repositories, vector
  store); the owner closes the service.
* **One documented order.** :data:`SHUTDOWN_ORDER` — stop new events, drain the
  worker, flush durable state, close the stores, drain the telemetry pool. A
  stage failure never skips the stages after it: losing buffered data and then
  leaking the connection is strictly worse than losing the data.
* **One deadline.** ``shutdown(timeout)`` bounds the whole call, so a commit
  that hangs cannot hold the process open.
* **No unearned durability.** A drain or flush that did not finish is reported
  through :class:`ShutdownReport` as structured diagnostics, never logged as a
  clean stop.

Concurrent *reload* — handing in-flight readers from one generation to the next
— belongs to :class:`~knowcode.generation_bundle.GenerationBundle` (Step 18).
This module owns process lifecycle; it drives the service's public ownership
interface and does not reach past it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from knowcode.telemetry import shutdown_telemetry
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Teardown stages, in the order they run. Every shutdown reports all of them,
#: including the ones that had nothing to do, so a report is a complete account
#: of the shutdown rather than a list of the parts that happened to act.
SHUTDOWN_ORDER: tuple[str, ...] = (
    "monitor",
    "worker",
    "flush",
    "stores",
    "telemetry",
)

#: Upper bound on a whole shutdown. Long enough for an in-flight embedding
#: transaction to finish, short enough that a hung one does not look like a
#: hung server.
DEFAULT_SHUTDOWN_TIMEOUT = 10.0


class OwnedService(Protocol):
    """The narrow service surface the server owner drives.

    Deliberately three methods: the owner starts a worker against a writer,
    makes durable state durable, and closes what the service opened. Everything
    else about the service is the request path's business, not shutdown's.
    """

    def watch_writer(self) -> Any:
        """Return the writer whose transactions the watch worker commits.

        A handle rather than an indexer since Step 18: it resolves the current
        generation per commit, so a reload during a watch session moves the
        worker onto the new generation instead of leaving it writing into a
        retired — and now closed — one.
        """

    def flush(self) -> None:
        """Persist in-memory index state that has a durable home."""

    def close(self) -> None:
        """Close every store and repository this service opened."""


class OwnedMonitor(Protocol):
    """The filesystem observer surface the owner drives."""

    def start(self) -> bool:
        """Begin watching. Returns whether this call started an observer."""

    def stop(self) -> None:
        """Stop watching and join the observer thread."""


@dataclass(frozen=True)
class StageOutcome:
    """What one teardown stage achieved."""

    name: str
    ok: bool
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        """Render as structured, non-sensitive diagnostics."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ShutdownReport:
    """A complete account of one shutdown.

    ``completed`` is true only when every stage succeeded *and* the worker
    drained everything it accepted. Anything else is named rather than implied:
    a caller that logs this report cannot accidentally claim durability the
    shutdown did not achieve.
    """

    completed: bool
    stages: tuple[StageOutcome, ...] = ()
    incomplete_work: tuple[str, ...] = ()

    def stage(self, name: str) -> Optional[StageOutcome]:
        """Return the outcome of one named stage, if it ran."""
        for outcome in self.stages:
            if outcome.name == name:
                return outcome
        return None

    @property
    def failed_stages(self) -> tuple[str, ...]:
        """Names of the stages that did not succeed, in shutdown order."""
        return tuple(outcome.name for outcome in self.stages if not outcome.ok)

    def as_dict(self) -> dict[str, Any]:
        """Render as structured, non-sensitive diagnostics.

        Carries stage names, outcomes, error text, and the repository paths
        that are not indexed as their events implied — the same surface the
        watch worker already exposes. No query text, credential, or config
        value passes through here.
        """
        return {
            "completed": self.completed,
            "failed_stages": list(self.failed_stages),
            "incomplete_work": list(self.incomplete_work),
            "stages": [outcome.as_dict() for outcome in self.stages],
        }


def _default_monitor_factory(root: Path, worker: Any) -> OwnedMonitor:
    """Build the production file monitor.

    Imported lazily so constructing a non-watching server never imports
    watchdog.
    """
    from knowcode.indexing.monitor import FileMonitor

    return FileMonitor(root, worker)


class ServerResources:
    """Owns every resource the API server creates, and releases them in order."""

    def __init__(
        self,
        service: OwnedService,
        *,
        watch: bool = False,
        watch_root: Optional[str | Path] = None,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        monitor_factory: Callable[[Path, Any], OwnedMonitor] = _default_monitor_factory,
    ) -> None:
        """Initialize the owner.

        Args:
            service: Service whose stores this owner closes.
            watch: Whether to run the file monitor and background worker.
            watch_root: Directory to watch. Required when ``watch`` is set.
            shutdown_timeout: Default bound on a whole :meth:`shutdown`.
            monitor_factory: Builds the observer; injectable so lifecycle tests
                do not depend on real filesystem events.
        """
        if watch and watch_root is None:
            raise ValueError("watch_root is required when watch is enabled")
        self.service = service
        self.watch = watch
        self.watch_root = Path(watch_root) if watch_root is not None else None
        self.shutdown_timeout = shutdown_timeout
        self._monitor_factory = monitor_factory
        self.worker: Any = None
        self.monitor: Optional[OwnedMonitor] = None
        self._entered = False
        self._report: Optional[ShutdownReport] = None

    # -- startup ---------------------------------------------------------

    def startup(self) -> None:
        """Start the watch resources. Idempotent.

        A second call is a no-op rather than a second worker over one queue
        (Step 16). If the observer cannot start, the worker started alongside it
        is stopped again before the error propagates, so a failed startup does
        not leave a consumer running against a server that never came up.
        """
        if self._report is not None:
            raise RuntimeError("These ServerResources were shut down; build new ones.")
        self._entered = True
        if not self.watch or self.worker is not None:
            return

        from knowcode.indexing.background_indexer import BackgroundIndexer

        worker = BackgroundIndexer(self.service.watch_writer())
        worker.start()
        self.worker = worker
        try:
            assert self.watch_root is not None  # guaranteed by __init__
            monitor = self._monitor_factory(self.watch_root, worker)
            monitor.start()
        except Exception:
            # Roll back to "nothing started" so shutdown has no half-wired
            # observer to reason about; the stores are still closed below.
            worker.stop(timeout=self.shutdown_timeout)
            self.worker = None
            raise
        self.monitor = monitor

    # -- shutdown --------------------------------------------------------

    def shutdown(self, timeout: Optional[float] = None) -> ShutdownReport:
        """Release every owned resource within one deadline. Idempotent.

        Args:
            timeout: Bound on the whole call. Defaults to ``shutdown_timeout``.

        Returns:
            The report of the shutdown that actually ran. A repeat call returns
            the first report rather than closing anything twice.
        """
        if self._report is not None:
            return self._report
        if not self._entered:
            # Nothing was ever started, so there is nothing to release. Recorded
            # so a later call still short-circuits.
            self._report = ShutdownReport(
                completed=True,
                stages=tuple(
                    StageOutcome(name, ok=True, detail="never started")
                    for name in SHUTDOWN_ORDER
                ),
            )
            return self._report

        deadline = time.monotonic() + (
            self.shutdown_timeout if timeout is None else timeout
        )
        stages: list[StageOutcome] = []
        incomplete: tuple[str, ...] = ()

        stages.append(self._stop_monitor())
        worker_stage, incomplete = self._drain_worker(deadline)
        stages.append(worker_stage)
        stages.append(self._run_stage("flush", self.service.flush))
        stages.append(self._run_stage("stores", self.service.close))
        stages.append(self._drain_telemetry(deadline))

        report = ShutdownReport(
            completed=all(stage.ok for stage in stages) and not incomplete,
            stages=tuple(stages),
            incomplete_work=incomplete,
        )
        self._report = report
        self._log(report)
        return report

    @property
    def shutdown_report(self) -> Optional[ShutdownReport]:
        """The report of a completed shutdown, or ``None`` while running."""
        return self._report

    # -- stages ----------------------------------------------------------

    def _stop_monitor(self) -> StageOutcome:
        """Stop new filesystem events before anything else is torn down."""
        monitor, self.monitor = self.monitor, None
        if monitor is None:
            return StageOutcome("monitor", ok=True, detail="not started")
        return self._run_stage("monitor", monitor.stop)

    def _drain_worker(self, deadline: float) -> tuple[StageOutcome, tuple[str, ...]]:
        """Drain queued work within the remaining budget.

        The worker's own :class:`~knowcode.indexing.background_indexer.
        DrainReport` already distinguishes committed work from work it could
        not commit, so this stage forwards that judgement rather than
        re-deriving it.
        """
        if self.worker is None:
            return StageOutcome("worker", ok=True, detail="not started"), ()
        try:
            drain = self.worker.stop(timeout=_remaining(deadline))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.exception("Draining the watch worker failed")
            return StageOutcome("worker", ok=False, detail=str(exc)), ()
        if drain.completed:
            return StageOutcome("worker", ok=True), ()
        incomplete = tuple(drain.incomplete_work)
        return (
            StageOutcome(
                "worker",
                ok=False,
                detail=f"{len(incomplete)} path(s) not indexed as their events implied",
            ),
            incomplete,
        )

    def _drain_telemetry(self, deadline: float) -> StageOutcome:
        """Drain the telemetry pool last: earlier stages may still log."""
        try:
            drained = shutdown_telemetry(timeout=_remaining(deadline))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.exception("Draining telemetry failed")
            return StageOutcome("telemetry", ok=False, detail=str(exc))
        if drained:
            return StageOutcome("telemetry", ok=True)
        return StageOutcome(
            "telemetry", ok=False, detail="pending writes were not drained"
        )

    @staticmethod
    def _run_stage(name: str, action: Callable[[], None]) -> StageOutcome:
        """Run one stage, converting failure into a reported outcome.

        Failures do not propagate: a stage that raises must not skip the stages
        after it, or a flush error would leave every connection open too.
        """
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.exception("Shutdown stage %r failed", name)
            return StageOutcome(name, ok=False, detail=str(exc))
        return StageOutcome(name, ok=True)

    @staticmethod
    def _log(report: ShutdownReport) -> None:
        """Emit the report at a level that matches what it says."""
        if report.completed:
            logger.info("Server shutdown complete: %s", report.as_dict())
        else:
            logger.warning("Server shutdown incomplete: %s", report.as_dict())


def _remaining(deadline: float) -> float:
    """Budget left before ``deadline``, never negative."""
    return max(0.0, deadline - time.monotonic())
