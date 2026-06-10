"""Unit tests for KnowCodeService.retrieve_context_for_query()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.data_models import CodeChunk, TaskType
from knowcode.errors import MissingKnowledgeStoreError, MissingSemanticIndexError
from knowcode.retrieval.search_engine import ScoredChunk
from knowcode.service import KnowCodeService


class DummySearchEngine:
    def __init__(self, scored: list[ScoredChunk] | Exception) -> None:
        self._scored = scored

    def search_scored(self, _query: str, **_kwargs) -> list[ScoredChunk]:  # type: ignore
        if isinstance(self._scored, Exception):
            raise self._scored
        return self._scored


class DummyService(KnowCodeService):
    def __init__(self, store_path: Path, engine: DummySearchEngine) -> None:
        super().__init__(store_path=store_path, app_config=AppConfig.default())
        self._engine = engine
        self.context_calls: list[tuple[str, int, TaskType]] = []
        self.search_calls: list[str] = []

    def get_search_engine(self, _index_path=None):  # type: ignore
        return self._engine

    def get_context(
        self,
        target: str,
        max_tokens: int = 2000,
        task_type: TaskType | None = None,
        summarize: bool = False,
    ):  # type: ignore
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

    def search(self, pattern: str):  # type: ignore
        self.search_calls.append(pattern)
        return [{"id": "e1"}, {"id": "e2"}]

    def _validate_index_compatibility(self, _index_path: Path) -> None:
        return


class BuilderTrackingService(KnowCodeService):
    def __init__(self, store_path: Path) -> None:
        super().__init__(store_path=store_path, app_config=AppConfig.default())
        self.analyze_calls = 0
        self.build_index_calls = 0

    def analyze(self, directory, output, ignore=None, temporal=False, coverage=None):  # type: ignore[override]
        self.analyze_calls += 1
        output_path = Path(output)
        output_path.mkdir(parents=True, exist_ok=True)
        store_file = output_path / "knowcode_knowledge.json"
        store_file.write_text(
            json.dumps({"metadata": {}, "entities": {}, "relationships": []}),
            encoding="utf-8",
        )
        return {}

    def _build_index(self, directory, index_path):  # type: ignore[override]
        self.build_index_calls += 1
        Path(index_path).mkdir(parents=True, exist_ok=True)
        return 0


def _write_store_file(path: Path) -> None:
    (path / "knowcode_knowledge.json").write_text(
        json.dumps({"metadata": {}, "entities": {}, "relationships": []}),
        encoding="utf-8",
    )


def test_retrieve_context_uses_semantic_when_index_exists(tmp_path: Path) -> None:
    _write_store_file(tmp_path)
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
    result = service.retrieve_context_for_query(
        "Explain e1", limit_entities=2, verbosity="diagnostic"
    )

    assert result["retrieval_mode"] == "semantic"
    assert [e["entity_id"] for e in result["selected_entities"]] == ["e1", "e2"]
    assert [c[0] for c in service.context_calls] == ["e1", "e2"]
    assert result["context_text"].count("CTX:") == 2


def test_retrieve_context_can_include_metadata_with_minimal_verbosity(
    tmp_path: Path,
) -> None:
    _write_store_file(tmp_path)
    (tmp_path / "knowcode_index").mkdir()

    chunk = CodeChunk(id="c1", entity_id="e1", content="one", tokens=["one"])
    service = DummyService(
        tmp_path,
        engine=DummySearchEngine(
            [ScoredChunk(chunk=chunk, score=0.9, source="retrieved")]
        ),
    )

    result = service.retrieve_context_for_query(
        "Explain e1",
        limit_entities=1,
        verbosity="minimal",
        include_metadata=True,
    )

    assert "reduction_summary" in result
    assert result["selected_entities"][0]["entity_id"] == "e1"
    assert result["evidence"][0]["chunk_id"] == "c1"
    assert result["retrieval_mode"] == "semantic"


def test_retrieve_context_falls_back_to_lexical_on_semantic_error(
    tmp_path: Path,
) -> None:
    _write_store_file(tmp_path)
    (tmp_path / "knowcode_index").mkdir()

    service = DummyService(
        tmp_path,
        engine=DummySearchEngine(RuntimeError("embed failed")),
    )
    result = service.retrieve_context_for_query(
        "Where is Foo defined?", limit_entities=1, verbosity="diagnostic"
    )

    assert result["retrieval_mode"] == "lexical"
    assert service.search_calls
    assert result["selected_entities"][0]["entity_id"] == "e1"


def test_retrieve_context_raises_when_store_missing(tmp_path: Path) -> None:
    service = DummyService(tmp_path, engine=DummySearchEngine([]))

    with pytest.raises(MissingKnowledgeStoreError) as exc:
        service.retrieve_context_for_query("Explain e1", verbosity="diagnostic")
    assert exc.value.code == "missing_knowledge_store"
    assert "knowcode analyze" in exc.value.hint


def test_retrieve_context_raises_when_index_missing(tmp_path: Path) -> None:
    _write_store_file(tmp_path)
    service = DummyService(tmp_path, engine=DummySearchEngine([]))

    with pytest.raises(MissingSemanticIndexError) as exc:
        service.retrieve_context_for_query("Explain e1", verbosity="diagnostic")
    assert exc.value.code == "missing_semantic_index"
    assert "knowcode index" in exc.value.hint


def test_ensure_store_builds_only_when_missing(tmp_path: Path) -> None:
    service = BuilderTrackingService(tmp_path)

    store_path = service.ensure_store()
    assert store_path.exists()
    assert service.analyze_calls == 1

    service.ensure_store()
    assert service.analyze_calls == 1


def test_ensure_index_builds_only_when_missing(tmp_path: Path) -> None:
    service = BuilderTrackingService(tmp_path)

    index_path = service.ensure_index()
    assert index_path.exists()
    assert service.build_index_calls == 1

    service.ensure_index()
    assert service.build_index_calls == 1
