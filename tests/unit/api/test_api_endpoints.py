"""Unit tests for API endpoint functions (no TestClient / sockets)."""

from __future__ import annotations

from typing import Any

from knowcode.api import api
from knowcode.data_models import CodeChunk


class DummyChunkRepo:
    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = {c.id: c for c in chunks}

    def get(self, chunk_id: str) -> CodeChunk | None:
        return self._chunks.get(chunk_id)


class DummySearchEngine:
    def __init__(self) -> None:
        self._chunks = [CodeChunk(id="c1", entity_id="e1", content="hi", tokens=["hi"])]
        self.chunk_repo = DummyChunkRepo(self._chunks)

    def search(self, query: Any, limit: Any=5, expand_deps: Any=True, **_kwargs: Any) -> Any:  # noqa: ANN001  # type: ignore
        return self._chunks


class DummyStore:
    def trace_calls(self, _entity_id: Any, direction: Any="callees", depth: Any=1, max_results: Any=50) -> Any:  # noqa: ANN001  # type: ignore
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

    def get_context(self, _target: Any, max_tokens: Any=2000, task_type: Any=None) -> Any:  # noqa: ANN001  # type: ignore
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
                {"rank": 1, "chunk_id": "c1", "entity_id": "e1", "score": 0.91, "source": "retrieved"}
            ],
        }

    def reload(self) -> Any:
        self.reload_called = True


def test_health_and_stats_endpoints() -> None:
    assert api.health() == {"status": "ok"}

    service = DummyService()
    stats = api.get_stats(service=service)  # type: ignore
    assert stats["total_entities"] == 1


def test_search_and_context_endpoints() -> None:
    service = DummyService()

    results = api.search(q="foo", service=service)  # type: ignore
    assert results[0]["id"] == "e1"

    context = api.get_context(
        target="e1",
        max_tokens=2000,
        task_type=api.TaskTypeParam.general,
        service=service,  # type: ignore
    )
    assert context["context_text"] == "ctx"


def test_query_and_entity_endpoints() -> None:
    service = DummyService()

    resp = api.query_context(api.QueryRequest(query="hi", limit=1), service=service)  # type: ignore
    assert resp.chunks[0].id == "c1"

    entity = api.get_entity(entity_id="e1", service=service)  # type: ignore
    assert entity["id"] == "e1"


def test_reload_endpoint() -> None:
    service = DummyService()
    resp = api.reload_store(service=service)  # type: ignore
    assert resp["status"] == "reloaded"
    assert service.reload_called is True


def test_trace_calls_and_impact_endpoints() -> None:
    service = DummyService()

    trace = api.trace_calls(
        entity_id="e1",
        direction=api.DirectionParam.callees,
        depth=1,
        max_results=50,
        service=service,  # type: ignore
    )
    assert trace[0]["entity_id"] == "e2"

    impact = api.get_impact(entity_id="e1", max_depth=3, service=service)  # type: ignore
    assert impact["entity_id"] == "e1"
