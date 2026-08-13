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
        self.retrieve_kwargs: list[dict[str, Any]] = []
        self.retrieval_result: dict | None = None  # type: ignore
        self.retrieval_results: list[dict[str, Any]] = []

    def retrieve_context_for_query(self, query: str, **kwargs: Any) -> Any:
        self.retrieve_calls.append(query)
        self.retrieve_kwargs.append(kwargs)
        if self.retrieval_results:
            return self.retrieval_results.pop(0)
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
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        local_answer_task_types=[TaskType.LOCATE.value, TaskType.EXPLAIN.value],
    )
    agent = Agent(service, cfg)  # type: ignore
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True

    stub_client = MagicMock()
    stub_client.models.generate_content.return_value = MagicMock(text="ANSWER")
    agent._get_client = MagicMock(return_value=stub_client)  # type: ignore
    return agent


def test_smart_answer_fails_closed_when_task_type_is_not_blessed(
    tmp_path: Path,
) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Where is Foo defined?",
        "task_type": TaskType.LOCATE.value,
        "task_confidence": 1.0,
        "context_text": "CTX:Foo",
        "sufficiency_score": 1.0,
    }
    cfg = AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                provider="google",
                api_key_env="TEST_KEY",
            )
        ]
    )
    agent = Agent(service, cfg)  # type: ignore[arg-type]
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]

    result = agent.smart_answer("Where is Foo defined?")

    assert result["source"] == "llm"
    assert result["task_type"] == TaskType.LOCATE.value
    assert result["routing_policy_allowed"] is False


def test_smart_answer_does_not_escalate_a_task_that_policy_cannot_route(
    tmp_path: Path,
) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "context_text": "CTX:Foo",
        "sufficiency_score": 0.1,
    }
    config = AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                provider="google",
                api_key_env="TEST_KEY",
            )
        ],
        local_answer_task_types=[],
    )
    agent = Agent(service, config)  # type: ignore[arg-type]
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]

    result = agent.smart_answer("Explain Foo")

    assert result["source"] == "llm"
    assert service.retrieve_calls == ["Explain Foo"]


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
    assert service.retrieve_kwargs == [
        {
            "max_tokens": 1500,
            "limit_entities": 1,
            "expand_deps": False,
            "verbosity": "minimal",
            "include_metadata": True,
        }
    ]


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
    assert service.retrieve_kwargs == [
        {
            "max_tokens": 1500,
            "limit_entities": 1,
            "expand_deps": False,
            "verbosity": "minimal",
            "include_metadata": True,
        }
    ]


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


def test_smart_answer_reuses_final_retrieval_for_llm_fallback(
    tmp_path: Path,
) -> None:
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
        "sufficiency_score": 0.5,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    agent = _make_agent(service)

    result = agent.smart_answer("Explain Foo")

    assert result["source"] == "llm"
    assert result["answer"] == "ANSWER"
    assert service.retrieve_calls == ["Explain Foo"] * 3
    assert service.retrieve_kwargs == [
        {
            "max_tokens": 1500,
            "limit_entities": 1,
            "expand_deps": False,
            "verbosity": "minimal",
            "include_metadata": True,
        },
        {
            "max_tokens": 3000,
            "limit_entities": 3,
            "expand_deps": True,
            "verbosity": "minimal",
            "include_metadata": True,
        },
        {
            "max_tokens": 3000,
            "limit_entities": 3,
            "expand_deps": True,
            "verbosity": "standard",
            "include_metadata": True,
        },
    ]


def test_smart_answer_stops_escalating_when_broader_context_is_sufficient(
    tmp_path: Path,
) -> None:
    service = DummyService(store_path=tmp_path)
    base_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "semantic",
        "total_tokens": 10,
        "max_tokens": 1500,
        "truncated": False,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    service.retrieval_results = [
        {**base_result, "context_text": "narrow", "sufficiency_score": 0.5},
        {**base_result, "context_text": "broad", "sufficiency_score": 0.9},
    ]
    agent = _make_agent(service)

    result = agent.smart_answer("Explain Foo")

    assert result["source"] == "local"
    assert result["context"] == "broad"
    assert service.retrieve_calls == ["Explain Foo", "Explain Foo"]
    assert [call["verbosity"] for call in service.retrieve_kwargs] == [
        "minimal",
        "minimal",
    ]


def test_smart_answer_force_llm_skips_retrieval_escalation(tmp_path: Path) -> None:
    service = DummyService(store_path=tmp_path)
    service.retrieval_result = {
        "query": "Explain Foo",
        "task_type": TaskType.EXPLAIN.value,
        "task_confidence": 1.0,
        "retrieval_mode": "semantic",
        "context_text": "CTX:Foo",
        "total_tokens": 10,
        "max_tokens": 1500,
        "truncated": False,
        "sufficiency_score": 0.5,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }
    agent = _make_agent(service)

    result = agent.smart_answer("Explain Foo", force_llm=True)

    assert result["source"] == "llm"
    assert service.retrieve_calls == ["Explain Foo"]


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
    # routing_quality_floor is zeroed to isolate the sufficiency_threshold knob
    # (otherwise the effective threshold is max(threshold, floor)).
    cfg = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.9,
        routing_quality_floor=0.0,
        local_answer_task_types=[TaskType.EXPLAIN.value],
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
        routing_quality_floor=0.0,
        local_answer_task_types=[TaskType.EXPLAIN.value],
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
        "sufficiency_score": 0.95,
        "selected_entities": [{"entity_id": "e1"}],
        "evidence": [],
        "errors": [],
    }

    agent = _make_agent(service)
    agent.answer = MagicMock(return_value="LLM")  # type: ignore[method-assign]
    
    _ = agent.smart_answer("Explain Foo")
    
    # Wait for async logging to complete in a robust retry loop
    import time
    log_file = tmp_path / "knowcode_telemetry.jsonl"
    for _ in range(40):
        if log_file.exists():
            try:
                content = log_file.read_text(encoding="utf-8").strip()
                if content:
                    break
            except Exception:
                pass
        time.sleep(0.05)
        
    assert log_file.exists()
    
    import json
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines]
    
    agent_decisions = [r for r in records if r.get("event_type") == "agent_decision"]
    assert len(agent_decisions) == 1
    assert agent_decisions[0]["query"] == "Explain Foo"
    assert agent_decisions[0]["source"] == "local"
    assert agent_decisions[0]["sufficiency_score"] == 0.95
    # The effective threshold is max(nominal, floor); both must be logged so the
    # floor's contribution to the decision is observable.
    assert agent_decisions[0]["sufficiency_threshold"] == 0.8
    assert agent_decisions[0]["routing_quality_floor"] == 0.9
    assert agent_decisions[0]["threshold"] == 0.9
