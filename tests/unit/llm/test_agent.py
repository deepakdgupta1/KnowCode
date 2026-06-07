"""Tests for the KnowCode Agent."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from unittest.mock import MagicMock

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import TaskType
from knowcode.llm.agent import Agent


class DummyService:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.retrieve_calls: list[str] = []
        self.retrieval_result: dict | None = None  # type: ignore

    def retrieve_context_for_query(self, query: str, **_kwargs) -> Any:  # type: ignore
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
            "max_tokens": 4000,
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
    agent = Agent(service, cfg)  # type: ignore
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True

    stub_client = MagicMock()
    stub_client.models.generate_content.return_value = MagicMock(text="ANSWER")
    agent._get_client = MagicMock(return_value=stub_client)  # type: ignore
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
        "max_tokens": 4000,
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
        "max_tokens": 4000,
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
        "max_tokens": 4000,
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


def test_smart_answer_respects_custom_config_threshold(tmp_path: Path) -> None:
    """Test that Agent.smart_answer respects AppConfig.sufficiency_threshold."""
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "semantic",
        "context_text": "CTX:Foo",
        "total_tokens": 10,
        "max_tokens": 4000,
        "truncated": False,
        "sufficiency_score": 0.85,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }

    # Set threshold to 0.9. Since sufficiency_score 0.85 < 0.9, it should call LLM.
    cfg = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.9,
    )
    agent = Agent(service, cfg)  # type: ignore
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True
    stub_client = MagicMock()
    stub_client.models.generate_content.return_value = MagicMock(text="ANSWER")
    agent._get_client = MagicMock(return_value=stub_client)  # type: ignore
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]

    result = agent.smart_answer("Explain Foo")
    assert result["source"] == "llm"

    # Set threshold to 0.8. Since sufficiency_score 0.85 >= 0.8, it should return local.
    cfg_local = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.8,
    )
    agent_local = Agent(service, cfg_local)  # type: ignore
    agent_local.rate_limiter = MagicMock()
    agent_local.rate_limiter.check_availability.return_value = True
    agent_local._get_client = MagicMock(return_value=stub_client)  # type: ignore

    result_local = agent_local.smart_answer("Explain Foo")
    assert result_local["source"] == "local"


def test_smart_answer_emits_telemetry(tmp_path: Path) -> None:
    """Test that Agent.smart_answer logs agent decisions to telemetry."""
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "semantic",
        "context_text": "CTX:Foo",
        "total_tokens": 10,
        "max_tokens": 4000,
        "truncated": False,
        "sufficiency_score": 0.85,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    
    agent = _make_agent(service)
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]
    
    _ = agent.smart_answer("Explain Foo")
    
    import time
    time.sleep(0.15)
    
    log_file = tmp_path / "knowcode_telemetry.jsonl"
    assert log_file.exists()
    
    import json
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines]
    
    agent_decisions = [r for r in records if r.get("event_type") == "agent_decision"]
    assert len(agent_decisions) == 1
    assert agent_decisions[0]["query"] == "Explain Foo"
    assert agent_decisions[0]["source"] == "local"
    assert agent_decisions[0]["sufficiency_score"] == 0.85




