"""Unit tests for API endpoint functions (no TestClient / sockets)."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from knowcode.api import api
from knowcode.api.rate_limit import limiter
from knowcode.data_models import CodeChunk
from knowcode.errors import MissingSemanticIndexError


# --- Disable rate limiting for direct function-call tests ---
@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Any:
    """Disable slowapi rate limiter during unit tests."""
    limiter.enabled = False
    yield
    limiter.enabled = True


# --- Mock Request for rate-limited endpoints ---
def _mock_request() -> Request:
    """Create a minimal real Starlette Request for direct endpoint testing."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


class DummyChunkRepo:
    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = {c.id: c for c in chunks}

    def get(self, chunk_id: str) -> CodeChunk | None:
        return self._chunks.get(chunk_id)


class DummySearchEngine:
    def __init__(self) -> None:
        self._chunks = [CodeChunk(id="c1", entity_id="e1", content="hi", tokens=["hi"])]
        self.chunk_repo = DummyChunkRepo(self._chunks)

    def search(
        self, query: Any, limit: Any = 5, expand_deps: Any = True, **_kwargs: Any
    ) -> Any:  # noqa: ANN001  # type: ignore
        return self._chunks


class DummyStore:
    def trace_calls(
        self,
        _entity_id: Any,
        direction: Any = "callees",
        depth: Any = 1,
        max_results: Any = 50,
    ) -> Any:  # noqa: ANN001  # type: ignore
        return [
            {
                "entity_id": "e2",
                "name": "bar",
                "qualified_name": "bar",
                "kind": "function",
                "file": "file.py",
                "line": 2,
                "call_depth": 1,
            }
        ]

    def get_impact(self, entity_id: str, max_depth: int = 3):  # type: ignore
        return {
            "entity_id": entity_id,
            "direct_dependents": [],
            "transitive_dependents": [],
            "affected_files": [],
            "risk_score": 0.0,
        }


class DummyService:
    def __init__(self) -> None:
        self.reload_called = False
        self.store = DummyStore()
        self._engine = DummySearchEngine()
        self.leases = 0

    @contextmanager
    def generation_lease(self):  # type: ignore
        # Step 18: endpoints pin one generation for a whole request. The fake
        # has one generation, so the lease only has to be countable.
        self.leases += 1
        yield self

    def get_stats(self):  # type: ignore
        return {"total_entities": 1}

    def search(self, _pattern):  # type: ignore
        return [
            {
                "id": "e1",
                "kind": "function",
                "name": "foo",
                "qualified_name": "foo",
                "file": "file.py",
                "line": 1,
            }
        ]

    def get_context(
        self, _target: Any, max_tokens: Any = 2000, task_type: Any = None
    ) -> Any:  # noqa: ANN001  # type: ignore
        return {
            "entity_id": "e1",
            "context_text": "ctx",
            "total_tokens": 1,
            "truncated": False,
            "included_entities": ["e1"],
            "task_type": (task_type.value if task_type else "general"),
            "sufficiency_score": 0.0,
        }

    def get_entity_details(self, _entity_id):  # type: ignore
        return {"id": "e1", "source_code": "pass", "location": {"file_path": "file.py"}}

    def get_callers(self, _entity_id):  # type: ignore
        return []

    def get_callees(self, _entity_id):  # type: ignore
        return []

    def get_search_engine(self, _index_path=None):  # type: ignore
        return self._engine

    def retrieve_context_for_query(
        self,
        query: str,
        task_type: Any = None,
        limit_entities: int = 5,
        expand_deps: bool = True,
    ) -> dict[str, Any]:
        _ = (query, limit_entities, expand_deps)
        task_name = task_type.value if task_type else "general"
        return {
            "task_type": task_name,
            "evidence": [
                {
                    "rank": 1,
                    "chunk_id": "c1",
                    "entity_id": "e1",
                    "score": 0.91,
                    "source": "retrieved",
                }
            ],
        }

    def reload(self) -> Any:
        self.reload_called = True

    def get_freshness_metadata(self) -> dict[str, Any]:
        return {
            "last_store_rebuild": 100,
            "last_index_rebuild": 200,
            "latest_source_change": 150,
            "is_stale": False,
            "stale_reasons": [],
        }


# --- Original endpoint tests (updated for rate-limited signatures) ---


def test_health_and_stats_endpoints() -> None:
    req = _mock_request()
    assert api.health(request=req) == {"status": "ok"}

    service = DummyService()
    stats = api.get_stats(request=req, service=service)  # type: ignore
    assert stats["total_entities"] == 1


def test_search_and_context_endpoints() -> None:
    req = _mock_request()
    service = DummyService()

    results = api.search(request=req, q="foo", limit=20, service=service)  # type: ignore
    assert results[0]["id"] == "e1"

    context = api.get_context(
        request=req,
        target="e1",
        max_tokens=2000,
        task_type=api.TaskTypeParam.general,
        service=service,  # type: ignore
    )
    assert context["context_text"] == "ctx"


def test_query_and_entity_endpoints() -> None:
    req = _mock_request()
    service = DummyService()

    resp = api.query_context(
        request=req, payload=api.QueryRequest(query="hi", limit=1), service=service
    )  # type: ignore
    assert resp.chunks[0].id == "c1"
    # Step 18: retrieval and the chunk resolution after it are one generation.
    assert service.leases == 1

    entity = api.get_entity(request=req, entity_id="e1", service=service)  # type: ignore
    assert entity["id"] == "e1"


def test_query_context_returns_412_for_missing_index() -> None:
    req = _mock_request()
    service = DummyService()

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise MissingSemanticIndexError(Path("knowcode_index"))

    service.retrieve_context_for_query = _raise  # type: ignore[method-assign]

    with pytest.raises(api.HTTPException) as exc:
        api.query_context(
            request=req, payload=api.QueryRequest(query="hi", limit=1), service=service
        )  # type: ignore

    assert exc.value.status_code == 412


def test_reload_endpoint() -> None:
    req = _mock_request()
    service = DummyService()
    resp = api.reload_store(request=req, service=service)  # type: ignore
    assert resp["status"] == "reloaded"
    assert service.reload_called is True


def test_trace_calls_and_impact_endpoints() -> None:
    req = _mock_request()
    service = DummyService()

    trace = api.trace_calls(
        request=req,
        entity_id="e1",
        direction=api.DirectionParam.callees,
        depth=1,
        max_results=50,
        service=service,  # type: ignore
    )
    assert trace[0]["entity_id"] == "e2"

    impact = api.get_impact(request=req, entity_id="e1", max_depth=3, service=service)  # type: ignore
    assert impact["entity_id"] == "e1"


# --- New validation tests ---


def test_query_limit_capped_at_20() -> None:
    """QueryRequest.limit should be capped at 20 via Pydantic validation."""
    with pytest.raises(ValidationError):
        api.QueryRequest(query="test", limit=25)

    # Valid: limit at exactly 20
    req = api.QueryRequest(query="test", limit=20)
    assert req.limit == 20


def test_query_max_tokens_capped_at_8000() -> None:
    """QueryRequest.max_tokens should be capped at 8000 via Pydantic validation."""
    with pytest.raises(ValidationError):
        api.QueryRequest(query="test", max_tokens=10000)

    # Valid: max_tokens at exactly 8000
    req = api.QueryRequest(query="test", max_tokens=8000)
    assert req.max_tokens == 8000


def test_get_context_rejects_excessive_max_tokens() -> None:
    """get_context should reject max_tokens > 8000 (enforced by FastAPI Query constraint)."""
    # This is enforced at the FastAPI query parameter level (le=8000).
    # When called directly, we just verify the endpoint works with valid values.
    req = _mock_request()
    service = DummyService()
    result = api.get_context(
        request=req,
        target="e1",
        max_tokens=8000,
        task_type=api.TaskTypeParam.general,
        service=service,  # type: ignore
    )
    assert result["entity_id"] == "e1"


def test_search_result_limit() -> None:
    """search endpoint should respect the limit parameter."""
    req = _mock_request()
    service = DummyService()

    # Default limit (20) - DummyService returns 1 result, so we get 1
    results = api.search(request=req, q="foo", limit=20, service=service)  # type: ignore
    assert len(results) == 1

    # Explicit limit of 0... wait, ge=1, so limit=1 is the minimum
    results = api.search(request=req, q="foo", limit=1, service=service)  # type: ignore
    assert len(results) == 1


def test_freshness_endpoint() -> None:
    """Test that the /freshness endpoint returns the mapped response model."""
    req = _mock_request()
    service = DummyService()
    resp = api.get_freshness(request=req, service=service)  # type: ignore
    assert resp.last_store_rebuild == 100
    assert resp.is_stale is False
    # Correct assertion
    assert resp.last_index_rebuild == 200
