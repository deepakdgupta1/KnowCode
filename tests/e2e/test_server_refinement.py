"""API refinement tests using the real service (no sockets/TestClient)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from knowcode.api import api
from knowcode.api.rate_limit import limiter
from knowcode.service import KnowCodeService


def _mock_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> None:
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(scope="module")
def service() -> KnowCodeService:
    return KnowCodeService(store_path=".")


def test_reload_endpoint(service: KnowCodeService) -> None:
    resp = api.reload_store(request=_mock_request(), service=service)
    assert resp["status"] == "reloaded"


def test_get_entity(service: KnowCodeService) -> None:
    req = _mock_request()
    results = api.search(request=req, q="GraphBuilder", limit=20, service=service)
    assert len(results) > 0

    entity_id = results[0]["id"]
    details = api.get_entity(request=req, entity_id=entity_id, service=service)

    assert details["id"] == entity_id
    assert "source_code" in details
    assert "location" in details


def test_entity_not_found(service: KnowCodeService) -> None:
    with pytest.raises(HTTPException):
        api.get_entity(
            request=_mock_request(),
            entity_id="non_existent_id",
            service=service,
        )
