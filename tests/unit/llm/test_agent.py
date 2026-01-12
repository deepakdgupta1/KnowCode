"""Tests for the KnowCode Agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import TaskType
from knowcode.llm.agent import Agent


class DummyService:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.retrieve_calls: list[str] = []
        self.retrieval_result: dict | None = None

    def retrieve_context_for_query(self, query: str, **_kwargs):
        self.retrieve_calls.append(query)
        if self.retrieval_result is not None:
            return self.retrieval_result
        return {
            "query": query,
            "task_type": TaskType.GENERAL.value,
            "task_confidence": 0.0,
            "retrieval_mode": "none",
            "context_text": "CTX",
            "total_tokens": 1,
            "max_tokens": 6000,
            "truncated": False,
            "sufficiency_score": 0.0,
            "selected_entities": [],
            "evidence": [],
            "errors": [],
        }


def _make_agent(service: DummyService) -> Agent:
    cfg = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")]
    )
    agent = Agent(service, cfg)
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True

    stub_client = MagicMock()
    stub_client.models.generate_content.return_value = MagicMock(text="ANSWER")
    agent._get_client = MagicMock(return_value=stub_client)
    return agent


def test_agent_answer_uses_unified_retrieval_kernel(tmp_path: Path) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain e1",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "semantic",
        "context_text": "CTX:e1",
        "total_tokens": 10,
        "max_tokens": 6000,
        "truncated": False,
        "sufficiency_score": 0.9,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    agent = _make_agent(service)

    answer = agent.answer("Explain e1")
    assert answer == "ANSWER"
    assert service.retrieve_calls == ["Explain e1"]


def test_smart_answer_uses_local_when_sufficient(tmp_path: Path) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Where is Foo defined?",
        "task_type": TaskType.LOCATE.value,
        "task_confidence": 1.0,
        "retrieval_mode": "lexical",
        "context_text": "CTX:Foo",
        "total_tokens": 10,
        "max_tokens": 6000,
        "truncated": False,
        "sufficiency_score": 1.0,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    agent = _make_agent(service)

    result = agent.smart_answer("Where is Foo defined?")
    assert result["source"] == "local"
    assert result["task_type"] == TaskType.LOCATE.value


def test_smart_answer_calls_llm_when_insufficient(tmp_path: Path) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "none",
        "context_text": "",
        "total_tokens": 0,
        "max_tokens": 6000,
        "truncated": False,
        "sufficiency_score": 0.0,
        "selected_entities": [],
        "evidence": [],
        "errors": [],
    }
    agent = _make_agent(service)
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]

    result = agent.smart_answer("Explain Foo")
    assert result["source"] == "llm"
    assert result["answer"] == "LLM"
