"""Unit tests for dependency readiness checks."""

from __future__ import annotations

from typing import Any, Dict, List

from agent_gateway.orchestrator import AgentOrchestrator
from agent_gateway.settings import GatewaySettings


class _DummyKnowCode:
    def __init__(self, payload: Dict[str, Any] | None = None, error: Exception | None = None):
        self._payload = payload or {"status": "ok"}
        self._error = error

    def check_health(self) -> Dict[str, Any]:
        if self._error:
            raise self._error
        return self._payload


class _DummyLiteLLM:
    def __init__(self, payload: Dict[str, Any] | None = None, error: Exception | None = None):
        self._payload = payload or {"status": "healthy"}
        self._error = error

    def check_health(self) -> Dict[str, Any]:
        if self._error:
            raise self._error
        return self._payload


class _DummyTools:
    def __init__(self, tools: List[Dict[str, Any]] | None = None, error: Exception | None = None):
        self._tools = tools or [{"type": "function", "function": {"name": "search"}}]
        self._error = error

    def get_tools(self, _selected_names: List[str]) -> List[Dict[str, Any]]:
        if self._error:
            raise self._error
        return self._tools


def _settings() -> GatewaySettings:
    return GatewaySettings(
        knowcode_api_base_url="http://127.0.0.1:8000",
        litellm_base_url="http://127.0.0.1:4000",
        litellm_api_key="sk-test",
        default_model="gemini/gemini-3-flash-preview",
        max_tool_rounds=4,
        tool_timeout_seconds=30.0,
        openapi_cache_ttl_seconds=300,
        allowed_tool_names=("query_context", "search"),
        default_tags=("knowcode", "context"),
        strict_env_validation=False,
    )


def test_readiness_ok_when_all_dependencies_healthy() -> None:
    orchestrator = AgentOrchestrator(settings=_settings())
    orchestrator._knowcode = _DummyKnowCode()  # type: ignore[assignment]
    orchestrator._litellm = _DummyLiteLLM()  # type: ignore[assignment]
    orchestrator._tools = _DummyTools()  # type: ignore[assignment]

    report = orchestrator.readiness()

    assert report["status"] == "ok"
    assert report["dependencies"]["knowcode_api"]["status"] == "ok"
    assert report["dependencies"]["litellm"]["status"] == "ok"
    assert report["dependencies"]["openapi_registry"]["status"] == "ok"


def test_readiness_error_when_any_dependency_fails() -> None:
    orchestrator = AgentOrchestrator(settings=_settings())
    orchestrator._knowcode = _DummyKnowCode()  # type: ignore[assignment]
    orchestrator._litellm = _DummyLiteLLM(error=RuntimeError("lite failed"))  # type: ignore[assignment]
    orchestrator._tools = _DummyTools()  # type: ignore[assignment]

    report = orchestrator.readiness()

    assert report["status"] == "error"
    assert report["dependencies"]["litellm"]["status"] == "error"

