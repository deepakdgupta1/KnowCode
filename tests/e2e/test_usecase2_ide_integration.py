"""E2E test for Use-Case 2: IDE Agent Integration.

Simulates the full workflow:
1. IDE agent invokes retrieve_context_for_query via MCP
2. KnowCode returns context bundle + sufficiency score
3. High sufficiency → local answer (zero external tokens)
4. Low sufficiency → LLM fallback
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import TaskType
from knowcode.llm.agent import Agent
from knowcode.mcp.server import KnowCodeMCPServer


class MockService:
    """Mock service simulating full retrieval pipeline."""

    def __init__(self, store_path: Path, sufficiency: float = 0.9) -> None:
        self.store_path = store_path
        self.sufficiency = sufficiency
        self.retrieve_calls: list[str] = []

    def retrieve_context_for_query(self, query: str, **kwargs):  # noqa: ANN001
        self.retrieve_calls.append(query)
        return {
            "query": query,
            "task_type": TaskType.EXPLAIN.value,
            "task_confidence": 0.95,
            "retrieval_mode": "semantic",
            "context_text": f"# Context for: {query}\n\ndef example_function():\n    pass",
            "total_tokens": 50,
            "max_tokens": kwargs.get("max_tokens", 6000),
            "truncated": False,
            "sufficiency_score": self.sufficiency,
            "selected_entities": [{"entity_id": "e1", "sufficiency_score": self.sufficiency}],
            "evidence": [{"rank": 1, "entity_id": "e1", "score": 0.9}],
            "errors": [],
        }


def test_usecase2_high_sufficiency_uses_local_answer(tmp_path: Path) -> None:
    """UC2 E2E: High sufficiency (>=0.8) should use local answer without LLM.
    
    Verifies that when context sufficiency is high, the agent returns
    a local answer and does not call any external LLM.
    """
    # Setup: Create mock service with high sufficiency
    mock_service = MockService(tmp_path, sufficiency=0.95)
    config = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.8,
    )
    
    agent = Agent(mock_service, config)  # type: ignore[arg-type]
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True
    
    # Track if LLM was called
    llm_called = False
    original_get_client = agent._get_client
    def mock_get_client(cfg):
        nonlocal llm_called
        llm_called = True
        return original_get_client(cfg)
    agent._get_client = mock_get_client  # type: ignore[method-assign]
    
    # Execute: Call smart_answer (the local-first mode)
    result = agent.smart_answer("Explain the Agent class")
    
    # Verify: Should use local answer
    assert result["source"] == "local"
    assert result["sufficiency_score"] >= 0.8
    assert not llm_called, "LLM should NOT be called for high sufficiency"
    assert mock_service.retrieve_calls, "Retrieval should be called"


def test_usecase2_low_sufficiency_triggers_llm(tmp_path: Path) -> None:
    """UC2 E2E: Low sufficiency (<0.8) should trigger LLM fallback.
    
    Verifies that when context sufficiency is below threshold,
    the agent calls the external LLM with the retrieved context.
    """
    # Setup: Create mock service with low sufficiency
    mock_service = MockService(tmp_path, sufficiency=0.5)
    config = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.8,
    )
    
    agent = Agent(mock_service, config)  # type: ignore[arg-type]
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True
    
    # Mock the answer method to track calls
    agent.answer = MagicMock(return_value="LLM generated answer")  # type: ignore[method-assign]
    
    # Execute
    result = agent.smart_answer("Explain complex architecture")
    
    # Verify: Should use LLM
    assert result["source"] == "llm"
    assert result["answer"] == "LLM generated answer"
    assert agent.answer.called, "LLM should be called for low sufficiency"


def test_usecase2_mcp_tool_returns_sufficiency_score(tmp_path: Path) -> None:
    """UC2 E2E: MCP tool should return sufficiency score for IDE agent decisions.
    
    This is the key contract between KnowCode and IDE agents:
    the sufficiency_score allows agents to decide local vs LLM.
    """
    import json
    
    # Create dummy store file
    (tmp_path / "knowcode_knowledge.json").write_text("{}")
    
    server = KnowCodeMCPServer(store_path=tmp_path)
    mock_service = MockService(tmp_path, sufficiency=0.85)
    server._ensure_service = lambda allow_missing_store=False: mock_service  # type: ignore[method-assign]
    
    # Simulate IDE agent calling retrieve_context_for_query
    result = json.loads(
        server.handle_tool_call(
            "retrieve_context_for_query",
            {"query": "How does auth work?", "task_type": "auto", "max_tokens": 4000},
        )
    )
    
    # Verify: Response must include sufficiency_score for IDE agent
    assert "sufficiency_score" in result, "Response MUST include sufficiency_score"
    assert 0.0 <= result["sufficiency_score"] <= 1.0
    assert "context_text" in result
    assert "task_type" in result
    assert result["sufficiency_score"] == 0.85


def test_usecase2_graceful_degradation_on_empty_context(tmp_path: Path) -> None:
    """UC2 E2E: Empty context should result in low sufficiency → LLM fallback.
    
    Even when no entities are found, the system should gracefully
    degrade by returning zero sufficiency, prompting LLM usage.
    """
    # Setup: Mock service with empty context
    mock_service = MockService(tmp_path, sufficiency=0.0)
    mock_service.retrieve_context_for_query = lambda q, **kw: {  # type: ignore[method-assign]
        "query": q,
        "task_type": TaskType.GENERAL.value,
        "task_confidence": 0.0,
        "retrieval_mode": "none",
        "context_text": "",
        "total_tokens": 0,
        "max_tokens": kw.get("max_tokens", 6000),
        "truncated": False,
        "sufficiency_score": 0.0,
        "selected_entities": [],
        "evidence": [],
        "errors": ["No matching entities found"],
    }
    
    config = AppConfig(
        models=[ModelConfig(name="test-model", provider="google", api_key_env="TEST_KEY")],
        sufficiency_threshold=0.8,
    )
    
    agent = Agent(mock_service, config)  # type: ignore[arg-type]
    agent.rate_limiter = MagicMock()
    agent.rate_limiter.check_availability.return_value = True
    agent.answer = MagicMock(return_value="General guidance")  # type: ignore[method-assign]
    
    # Execute
    result = agent.smart_answer("What is foo?")
    
    # Verify: Should fall back to LLM due to zero sufficiency
    assert result["source"] == "llm"
    assert result["sufficiency_score"] == 0.0
