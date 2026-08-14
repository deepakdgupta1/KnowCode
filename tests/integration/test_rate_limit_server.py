"""Enabled-limiter integration tests against the real server stack (Step 21).

Unlike ``tests/unit/api`` and ``tests/e2e/test_server_refinement.py``, this suite
deliberately does **not** disable the SlowAPI limiter. It starts a real uvicorn
server over a loopback socket so the actual proxy-header decision and the actual
rate-limit enforcement are exercised, not a disabled decorator.

Two properties are proven end to end:

* **Proxy trust (ADR 6).** The normal local server disables proxy-header
  processing, so a direct local client cannot rotate the rate-limit bucket by
  spoofing ``X-Forwarded-For``/``Forwarded``. A negative control starts an
  intentionally trusting server and shows the same spoofing *does* rotate
  buckets there — proving the assertions are not vacuous.
* **Enforcement.** Requests share one bucket keyed on the real peer and return
  ``429`` after the documented limit, and the standard and expensive tiers are
  independent.

The suite also pins the SlowAPI regression that made ``POST
/api/v1/context/query`` unreachable: its ``request`` parameter was the pydantic
body model, not the Starlette ``Request`` the limiter decorator requires.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from typing import Any, Iterator

import pytest
import requests

from knowcode.api.rate_limit import (
    EXPENSIVE_LIMIT,
    STANDARD_LIMIT,
    limiter,
)


# --------------------------------------------------------------------------- #
# Test infrastructure
# --------------------------------------------------------------------------- #


def _limit_count(rule: str) -> int:
    """Parse the request count out of a ``"60/minute"`` style SlowAPI rule."""
    return int(rule.split("/", 1)[0])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running(server: Any) -> Iterator[None]:
    """Run a ``uvicorn.Server`` in a background thread for the block's duration."""
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20.0
    while not server.started and time.time() < deadline:
        if not thread.is_alive():  # startup crashed
            raise RuntimeError("uvicorn server thread exited before startup")
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5.0)
        raise RuntimeError("uvicorn server did not start within the deadline")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _insecure_server(app: Any, host: str, port: int) -> Any:
    """A server configured the way ``uvicorn.run(app)`` used to default.

    ``proxy_headers=True`` with localhost trusted is exactly the boundary ADR 6
    closes: it lets a direct 127.0.0.1 client rewrite its own client host from
    ``X-Forwarded-For``. Used only by the negative control.
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return uvicorn.Server(config)


@contextlib.contextmanager
def _live_server(store_path: str, *, secure: bool) -> Iterator[str]:
    """Start ``create_app`` under a real uvicorn server and yield its base URL."""
    from knowcode.api.main import build_server, create_app

    app = create_app(store_path=store_path)
    port = _free_port()
    server = (
        build_server(app, "127.0.0.1", port)
        if secure
        else _insecure_server(app, "127.0.0.1", port)
    )
    with _running(server):
        yield f"http://127.0.0.1:{port}"


def _hammer(
    base_url: str,
    path: str,
    count: int,
    *,
    header: str | None = None,
    rotate: bool = False,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
) -> list[int]:
    """Send ``count`` sequential requests and return their status codes.

    ``rotate`` gives every request a distinct forwarding-header value, which is
    the spoofing a direct client would attempt to escape its bucket.
    """
    statuses: list[int] = []
    for i in range(count):
        headers: dict[str, str] = {}
        if header is not None:
            headers[header] = f"203.0.113.{(i % 250) + 1}" if rotate else "203.0.113.7"
        resp = requests.request(
            method, f"{base_url}{path}", headers=headers, json=json_body, timeout=10.0
        )
        statuses.append(resp.status_code)
    return statuses


STANDARD = _limit_count(STANDARD_LIMIT)
EXPENSIVE = _limit_count(EXPENSIVE_LIMIT)


@pytest.fixture(scope="session")
def store_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build a minimal real index once so every endpoint answers 2xx/4xx."""
    from knowcode.service import KnowCodeService

    tmp_dir = tmp_path_factory.mktemp("rate_limit_store")
    (tmp_dir / "app.py").write_text(
        "class GraphBuilder:\n"
        "    def build(self):\n"
        "        return helper()\n\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    builder = KnowCodeService(store_path=str(tmp_dir))
    builder.ensure_store()
    builder.ensure_index()
    builder.close()
    return str(tmp_dir)


@pytest.fixture(autouse=True)
def _enable_and_reset_limiter() -> Iterator[None]:
    """Enable the limiter and clear its shared bucket state around each test.

    The limiter is a process-global singleton with in-memory storage shared with
    the server thread, so per-test isolation is a reset rather than a rebuild.
    """
    from knowcode.api import api

    previous = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.enabled = previous
        api._service = None  # a leaked global would bleed into the next test


# --------------------------------------------------------------------------- #
# Fix A — the /context/query endpoint is reachable under the enabled limiter
# --------------------------------------------------------------------------- #


def test_context_query_reachable_under_enabled_limiter(store_path: str) -> None:
    """POST /context/query must not 500 on the SlowAPI ``request`` parameter.

    The limiter decorator looks up the parameter *named* ``request`` and asserts
    it is a Starlette ``Request``. When that name held the pydantic body model,
    every call raised and the endpoint was unreachable.
    """
    from knowcode.api.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(store_path=store_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/context/query", json={"query": "builder", "limit": 1}
        )
    # 200 (answered) or 412 (no semantic index) are both fine; 500 is the bug.
    assert resp.status_code != 500, resp.text
    assert resp.status_code in (200, 412)


# --------------------------------------------------------------------------- #
# Enforcement — the documented limits actually reject
# --------------------------------------------------------------------------- #


def test_standard_bucket_enforced_at_documented_limit(store_path: str) -> None:
    with _live_server(store_path, secure=True) as base:
        statuses = _hammer(base, "/api/v1/health", STANDARD + 1)

    assert statuses[:STANDARD] == [200] * STANDARD
    assert statuses[STANDARD] == 429
    assert statuses.count(429) == 1


def test_expensive_bucket_independent_of_standard(store_path: str) -> None:
    with _live_server(store_path, secure=True) as base:
        expensive = _hammer(base, "/api/v1/impact/x", EXPENSIVE + 1)
        # The standard bucket is untouched by exhausting the expensive one.
        health = requests.get(f"{base}/api/v1/health", timeout=10.0).status_code

    assert expensive[:EXPENSIVE] == [200] * EXPENSIVE
    assert expensive[EXPENSIVE] == 429
    assert health == 200


def test_concurrent_requests_are_bounded_by_the_bucket(store_path: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    burst = EXPENSIVE + 15
    with _live_server(store_path, secure=True) as base:

        def _one(_: int) -> int:
            return requests.get(f"{base}/api/v1/impact/x", timeout=10.0).status_code

        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(_one, range(burst)))

    admitted = sum(1 for s in statuses if s != 429)
    # Fixed-window strategy never admits more than the limit within one window.
    assert admitted <= EXPENSIVE
    assert statuses.count(429) == burst - admitted
    assert statuses.count(429) >= 1


# --------------------------------------------------------------------------- #
# Security — spoofed forwarding headers cannot rotate the bucket (ADR 6)
# --------------------------------------------------------------------------- #


def test_spoofed_x_forwarded_for_shares_one_bucket(store_path: str) -> None:
    with _live_server(store_path, secure=True) as base:
        statuses = _hammer(
            base,
            "/api/v1/impact/x",
            EXPENSIVE + 1,
            header="X-Forwarded-For",
            rotate=True,
        )

    admitted = sum(1 for s in statuses if s != 429)
    # A fresh spoofed IP on every request buys nothing: one real peer, one bucket.
    assert admitted == EXPENSIVE
    assert statuses[-1] == 429


def test_spoofed_forwarded_header_shares_one_bucket(store_path: str) -> None:
    with _live_server(store_path, secure=True) as base:
        statuses = _hammer(
            base,
            "/api/v1/impact/x",
            EXPENSIVE + 1,
            header="Forwarded",
            rotate=True,
        )

    admitted = sum(1 for s in statuses if s != 429)
    assert admitted == EXPENSIVE
    assert statuses[-1] == 429


def test_insecure_proxy_config_rotates_buckets_control(store_path: str) -> None:
    """Negative control: the boundary ADR 6 closes is real and detectable.

    With ``proxy_headers`` trusting localhost, each spoofed ``X-Forwarded-For``
    becomes a distinct client host and therefore a distinct bucket, so far more
    than the limit is admitted without a single 429. This is exactly what the
    secure tests above prove cannot happen on the production server.
    """
    with _live_server(store_path, secure=False) as base:
        statuses = _hammer(
            base,
            "/api/v1/impact/x",
            (EXPENSIVE + 1) * 2,
            header="X-Forwarded-For",
            rotate=True,
        )

    assert statuses.count(429) == 0
    assert statuses.count(200) == (EXPENSIVE + 1) * 2


# --------------------------------------------------------------------------- #
# The production server object is hardened by construction
# --------------------------------------------------------------------------- #


def test_production_server_disables_proxy_headers(store_path: str) -> None:
    from knowcode.api.main import build_server, create_app

    app = create_app(store_path=store_path)
    server = build_server(app, "127.0.0.1", _free_port())

    assert server.config.proxy_headers is False
    # No proxy is trusted: even if proxy processing were re-enabled, the default
    # is to trust nobody rather than falling back to localhost.
    assert server.config.forwarded_allow_ips == []
