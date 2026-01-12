"""API refinement tests using the real service (no sockets/TestClient)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from knowcode.api import api
from knowcode.service import KnowCodeService


@pytest.fixture(scope="module")
def service() -> KnowCodeService:
    return KnowCodeService(store_path=".")


def test_reload_endpoint(service: KnowCodeService) -> None:
    resp = api.reload_store(service=service)
    assert resp["status"] == "reloaded"


def test_get_entity(service: KnowCodeService) -> None:
    results = api.search(q="GraphBuilder", service=service)
    assert len(results) > 0

    entity_id = results[0]["id"]
    details = api.get_entity(entity_id=entity_id, service=service)

    assert details["id"] == entity_id
    assert "source_code" in details
    assert "location" in details


def test_entity_not_found(service: KnowCodeService) -> None:
    with pytest.raises(HTTPException):
        api.get_entity(entity_id="non_existent_id", service=service)
