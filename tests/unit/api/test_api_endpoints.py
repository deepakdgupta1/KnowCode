"""Unit tests for FastAPI endpoints using TestClient."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowcode.api import api
from knowcode.models import CodeChunk


class DummySearchEngine:
    def search(self, query, limit=5, expand_deps=True, **_kwargs):
        return [CodeChunk(id="c1", entity_id="e1", content="hi", tokens=["hi"])]


class DummyService:
    def __init__(self) -> None:
        self.reload_called = False

    def get_stats(self):
        return {"total_entities": 1}

    def search(self, _pattern):
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

    def get_context(self, _target, max_tokens=2000):
        return {
            "entity_id": "e1",
            "context_text": "ctx",
            "total_tokens": 1,
            "truncated": False,
            "included_entities": ["e1"],
        }

    def get_entity_details(self, _entity_id):
        return {"id": "e1", "source_code": "pass", "location": {"file_path": "file.py"}}

    def get_callers(self, _entity_id):
        return []

    def get_callees(self, _entity_id):
        return []

    def get_search_engine(self, _index_path=None):
        return DummySearchEngine()

    def reload(self):
        self.reload_called = True


def _make_client():
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_health_and_stats_endpoints():
    api._service = DummyService()
    client = _make_client()

    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/api/v1/stats").json()["total_entities"] == 1


def test_search_and_context_endpoints():
    api._service = DummyService()
    client = _make_client()

    search_resp = client.get("/api/v1/search", params={"q": "foo"})
    assert search_resp.status_code == 200
    assert search_resp.json()[0]["id"] == "e1"

    context_resp = client.get("/api/v1/context", params={"target": "e1"})
    assert context_resp.status_code == 200
    assert context_resp.json()["context_text"] == "ctx"


def test_query_and_entity_endpoints():
    api._service = DummyService()
    client = _make_client()

    query_resp = client.post("/api/v1/context/query", json={"query": "hi", "limit": 1})
    assert query_resp.status_code == 200
    assert query_resp.json()["chunks"][0]["id"] == "c1"

    entity_resp = client.get("/api/v1/entities/e1")
    assert entity_resp.status_code == 200
    assert entity_resp.json()["id"] == "e1"


def test_reload_endpoint():
    service = DummyService()
    api._service = service
    client = _make_client()

    resp = client.post("/api/v1/reload")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reloaded"
    assert service.reload_called is True
