"""Isolation between the shared read service and background lifecycle jobs.

The first implementation let a lifecycle job body call ``_ensure_service()``,
which returned the *same* cached instance the foreground thread was about to
close via ``_invalidate_service()``. The job's own work still published its
generation to disk, but the final ``adopt_generation`` step then raised
``RepositoryClosedError`` — so a build that had genuinely succeeded was
recorded as ``failed``, telling the agent to re-run a slow, quota-consuming
operation that did not need re-running.

A lifecycle job therefore owns a dedicated service that the foreground cache
never hands out and never closes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from knowcode.mcp.jobs import JobRegistry, JobState
from knowcode.mcp.server import KnowCodeMCPServer


def _repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "m.py").write_text(
        '"""Module."""\n\n\ndef f() -> int:\n    """Return one."""\n    return 1\n',
        encoding="utf-8",
    )


def _built_server(root: Path) -> KnowCodeMCPServer:
    """A server whose repository has one published generation."""
    _repo(root)
    server = KnowCodeMCPServer(root, jobs=JobRegistry(executor=lambda fn: fn()))
    status = json.loads(
        server.handle_tool_call("knowcode_lifecycle", {"action": "build"})
    )
    assert status["state"] in {"running", "succeeded"}
    return server


class TestDedicatedJobService:
    def test_job_service_is_not_the_shared_cached_instance(
        self, tmp_path: Path
    ) -> None:
        server = _built_server(tmp_path)
        shared = server._ensure_service()

        dedicated = server._dedicated_service()

        assert dedicated is not shared
        dedicated.close()

    def test_invalidating_the_cache_does_not_close_a_job_service(
        self, tmp_path: Path
    ) -> None:
        """The exact interleaving that produced the false failure."""
        server = _built_server(tmp_path)
        server._ensure_service()  # populate the foreground cache

        job_service = server._dedicated_service()
        server._invalidate_service()

        assert job_service.is_closed is False
        job_service.close()

    def test_a_build_racing_a_read_still_reports_success(self, tmp_path: Path) -> None:
        """End-to-end: a concurrent read must not fail an in-flight build."""
        server = _built_server(tmp_path)
        started = threading.Event()
        release = threading.Event()

        def gated(fn: Any) -> None:
            def wrapper() -> None:
                started.set()
                release.wait(timeout=10)
                fn()

            threading.Thread(target=wrapper, daemon=True).start()

        server._jobs = JobRegistry(executor=gated, id_factory=lambda: "j-race")
        server.handle_tool_call("knowcode_lifecycle", {"action": "build"})
        assert started.wait(timeout=5)

        # A read while the job is queued populates the foreground cache; the
        # job must not be affected by it, nor by its later invalidation.
        server.handle_tool_call("knowcode_inspect", {"action": "stats"})
        release.set()
        assert server._jobs.wait("j-race", timeout=120)

        job = server._jobs.get("j-race")
        assert job is not None
        assert job.state is JobState.SUCCEEDED, job.error
        assert job.result is not None
        assert job.result["published"] is True

    def test_reads_after_a_build_see_the_new_generation(self, tmp_path: Path) -> None:
        """The cache must not pin a pre-build generation for the session."""
        server = _built_server(tmp_path)
        before = json.loads(
            server.handle_tool_call("knowcode_inspect", {"action": "stats"})
        )

        # Add a second entity, then rebuild through the surface.
        (tmp_path / "src" / "n.py").write_text(
            '"""Second."""\n\n\ndef g() -> int:\n    """Return two."""\n    return 2\n',
            encoding="utf-8",
        )
        server.handle_tool_call("knowcode_lifecycle", {"action": "build"})

        after = json.loads(
            server.handle_tool_call("knowcode_inspect", {"action": "stats"})
        )
        assert after["total_entities"] > before["total_entities"]


class TestConcurrentEnsureService:
    def test_concurrent_callers_share_one_instance(self, tmp_path: Path) -> None:
        """An unlocked check-then-create leaked an unclosed service."""
        server = _built_server(tmp_path)
        server._invalidate_service()

        seen: list[Any] = []
        barrier = threading.Barrier(4)

        def grab() -> None:
            barrier.wait(timeout=10)
            seen.append(server._ensure_service())

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(seen) == 4
        assert len({id(service) for service in seen}) == 1
