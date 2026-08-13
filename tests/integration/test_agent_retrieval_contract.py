"""Agent integration tests against the production retrieval projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import CodeChunk, TaskType
from knowcode.llm.agent import Agent
from knowcode.retrieval.orchestrator import RetrievalOrchestrator
from knowcode.retrieval.search_engine import ScoredChunk


class ContractSearchEngine:
    """Return one deterministic entity through the orchestrator search path."""

    def search_scored(
        self,
        _query: str,
        limit: int = 10,
        expand_deps: bool = True,
    ) -> list[ScoredChunk]:
        del limit, expand_deps
        chunk = CodeChunk(
            id="chunk:foo",
            entity_id="function:foo",
            content="def foo(): ...",
            tokens=["foo"],
        )
        return [ScoredChunk(chunk=chunk, score=0.95, source="retrieved")]


class ContractService:
    """Expose a real RetrievalOrchestrator response to Agent."""

    def __init__(self, store_path: Path, sufficiency: float) -> None:
        self.store_path = store_path
        self.sufficiency = sufficiency
        self.retrieve_calls = 0
        self._retrieval_orchestrator = RetrievalOrchestrator(self)

    def retrieve_context_for_query(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.retrieve_calls += 1
        return self._retrieval_orchestrator.retrieve_context_for_query(
            query=query,
            **kwargs,
        )

    def _assert_store_exists(self) -> Path:
        return self.store_path

    def _assert_index_exists(self) -> Path:
        return self.store_path / "knowcode_index"

    def _validate_index_compatibility(self, _index_path: Path) -> None:
        return

    def get_search_engine(self, _index_path: Path) -> ContractSearchEngine:
        return ContractSearchEngine()

    def get_exact_query_engine(self, _index_path: Path) -> ContractSearchEngine:
        return ContractSearchEngine()

    def search(self, _pattern: str) -> list[dict[str, Any]]:
        return []

    def _extract_query_keywords(self, _query: str) -> list[str]:
        return []

    def get_context(
        self,
        target: str,
        max_tokens: int = 2000,
        task_type: TaskType | None = None,
        summarize: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        del max_tokens, summarize, is_stale
        assert task_type is not None
        return {
            "entity_id": target,
            "context_text": "foo is defined in src/example.py",
            "total_tokens": 8,
            "truncated": False,
            "task_type": task_type.value,
            "sufficiency_score": self.sufficiency,
        }


def _make_agent(service: ContractService, threshold: float = 0.8, floor: float = 0.9) -> Agent:
    config = AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                provider="google",
                api_key_env="TEST_KEY",
            )
        ],
        sufficiency_threshold=threshold,
        routing_quality_floor=floor,
        local_answer_task_types=[TaskType.LOCATE.value, TaskType.EXPLAIN.value],
    )
    agent = Agent(service, config)  # type: ignore[arg-type]
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text="LLM answer")
    agent._get_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    return agent


def test_agent_reads_task_metadata_from_minimal_projection(tmp_path: Path) -> None:
    service = ContractService(tmp_path, sufficiency=0.95)
    agent = _make_agent(service)

    result = agent.smart_answer("Where is foo defined?")

    assert result["source"] == "local"
    assert result["task_type"] == TaskType.LOCATE.value
    assert service.retrieve_calls == 1


def test_agent_routes_minimal_projection_to_llm_once(tmp_path: Path) -> None:
    service = ContractService(tmp_path, sufficiency=0.5)
    agent = _make_agent(service)

    result = agent.smart_answer("Explain foo")

    assert result["source"] == "llm"
    assert result["task_type"] == TaskType.EXPLAIN.value
    assert result["answer"] == "LLM answer"
    assert service.retrieve_calls == 3


def test_routing_quality_floor_overrides_a_lower_nominal_threshold(
    tmp_path: Path,
) -> None:
    """The adjudicated floor is a real runtime gate, not load-time provenance.

    Sufficiency 0.90 clears the nominal threshold (0.85) but falls below the
    floor (0.95), so the effective threshold is 0.95 and the query must route
    to the LLM rather than be answered locally.
    """
    service = ContractService(tmp_path, sufficiency=0.90)
    agent = _make_agent(service, threshold=0.85, floor=0.95)

    result = agent.smart_answer("Explain foo")

    assert result["source"] == "llm"
    assert result["sufficiency_score"] == 0.90
