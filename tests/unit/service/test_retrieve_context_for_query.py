"""Unit tests for KnowCodeService.retrieve_context_for_query()."""

from __future__ import annotations

from pathlib import Path

from knowcode.config import AppConfig
from knowcode.data_models import CodeChunk, TaskType
from knowcode.retrieval.search_engine import ScoredChunk
from knowcode.service import KnowCodeService


class DummySearchEngine:
    def __init__(self, scored: list[ScoredChunk] | Exception) -> None:
        self._scored = scored

    def search_scored(self, _query: str, **_kwargs) -> list[ScoredChunk]:
        if isinstance(self._scored, Exception):
            raise self._scored
        return self._scored


class DummyService(KnowCodeService):
    def __init__(self, store_path: Path, engine: DummySearchEngine) -> None:
        super().__init__(store_path=store_path, app_config=AppConfig.default())
        self._engine = engine
        self.context_calls: list[tuple[str, int, TaskType]] = []
        self.search_calls: list[str] = []

    def get_search_engine(self, _index_path=None):  # type: ignore[override]
        return self._engine

    def get_context(self, target: str, max_tokens: int = 2000, task_type: TaskType | None = None):  # type: ignore[override]
        assert task_type is not None
        self.context_calls.append((target, max_tokens, task_type))
        return {
            "entity_id": target,
            "context_text": f"CTX:{target}",
            "total_tokens": 10,
            "truncated": False,
            "included_entities": [target],
            "task_type": task_type.value,
            "sufficiency_score": 1.0,
        }

    def search(self, pattern: str):  # type: ignore[override]
        self.search_calls.append(pattern)
        return [{"id": "e1"}, {"id": "e2"}]

    def _validate_index_compatibility(self, _index_path: Path) -> None:  # type: ignore[override]
        return


def test_retrieve_context_uses_semantic_when_index_exists(tmp_path: Path) -> None:
    (tmp_path / "knowcode_index").mkdir()

    chunk_a = CodeChunk(id="c1", entity_id="e1", content="one", tokens=["one"])
    chunk_b = CodeChunk(id="c2", entity_id="e2", content="two", tokens=["two"])
    scored = [
        ScoredChunk(chunk=chunk_a, score=0.9, source="retrieved"),
        ScoredChunk(chunk=chunk_a, score=0.8, source="retrieved"),  # dup entity
        ScoredChunk(chunk=chunk_b, score=0.7, source="retrieved"),
        ScoredChunk(chunk=chunk_b, score=0.0, source="dependency"),
    ]

    service = DummyService(tmp_path, engine=DummySearchEngine(scored))
    result = service.retrieve_context_for_query("Explain e1", limit_entities=2)

    assert result["retrieval_mode"] == "semantic"
    assert [e["entity_id"] for e in result["selected_entities"]] == ["e1", "e2"]
    assert [c[0] for c in service.context_calls] == ["e1", "e2"]
    assert result["context_text"].count("CTX:") == 2


def test_retrieve_context_falls_back_to_lexical_on_semantic_error(tmp_path: Path) -> None:
    (tmp_path / "knowcode_index").mkdir()

    service = DummyService(
        tmp_path,
        engine=DummySearchEngine(RuntimeError("embed failed")),
    )
    result = service.retrieve_context_for_query("Where is Foo defined?", limit_entities=1)

    assert result["retrieval_mode"] == "lexical"
    assert service.search_calls
    assert result["selected_entities"][0]["entity_id"] == "e1"
