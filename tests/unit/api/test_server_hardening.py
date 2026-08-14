"""Unit tests for the direct-server proxy-trust configuration (Step 21, ADR 6).

These exercise the server *construction* without opening a socket. The behavioral
proof that spoofed forwarding headers cannot rotate the rate-limit bucket lives in
``tests/integration/test_rate_limit_server.py``, which drives a real server stack.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI

from knowcode.api import main


def test_proxy_trust_constants_reject_wildcard() -> None:
    """The direct server trusts no proxy and never wildcards forwarded headers."""
    assert main.PROXY_HEADERS_ENABLED is False
    assert main.TRUSTED_PROXY_IPS == []


def test_build_server_disables_proxy_headers() -> None:
    """``build_server`` produces a uvicorn config that ignores forwarded headers."""
    app = FastAPI()
    server = main.build_server(app, "127.0.0.1", 8123)

    assert server.config.proxy_headers is False
    # Empty (not None): had this been None, uvicorn would fall back to trusting
    # localhost, which is exactly the boundary ADR 6 closes.
    assert server.config.forwarded_allow_ips == []
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8123


def test_build_server_does_not_alias_the_trusted_ip_list() -> None:
    """Each server gets its own list, so a future mutation cannot leak globally."""
    app = FastAPI()
    server = main.build_server(app, "127.0.0.1", 8124)

    assert server.config.forwarded_allow_ips is not main.TRUSTED_PROXY_IPS


def test_start_server_routes_through_the_hardened_builder(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``start_server`` must build the app and serve it through ``build_server``.

    A regression that called ``uvicorn.run(app)`` directly would reintroduce the
    default proxy-header trust, so the wiring itself is pinned.
    """
    sentinel_app = object()
    fake_server = MagicMock()
    create_app = MagicMock(return_value=sentinel_app)
    build_server = MagicMock(return_value=fake_server)

    monkeypatch.setattr(main, "create_app", create_app)
    monkeypatch.setattr(main, "build_server", build_server)

    main.start_server(host="1.2.3.4", port=9999, store_path="/tmp/x", watch=True)

    create_app.assert_called_once_with(store_path="/tmp/x", watch=True)
    build_server.assert_called_once_with(sentinel_app, host="1.2.3.4", port=9999)
    fake_server.run.assert_called_once_with()
