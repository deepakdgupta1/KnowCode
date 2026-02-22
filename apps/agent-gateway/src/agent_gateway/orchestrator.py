"""Core orchestration loop for tool-enabled reasoning through LiteLLM."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent_gateway.knowcode_client import KnowCodeClient, ToolExecutionError
from agent_gateway.litellm_client import LiteLLMClient, LiteLLMError
from agent_gateway.models import ChatRequest, ChatResponse, ToolExecutionRecord, model_dump
from agent_gateway.openapi_tools import OpenAPIToolRegistry
from agent_gateway.settings import GatewaySettings
from agent_gateway.tool_selector import select_tool_names


@dataclass
class AgentOrchestrator:
    """Coordinates model inference and tool execution loops."""

    settings: GatewaySettings

    def __post_init__(self) -> None:
        self._tools = OpenAPIToolRegistry(
            knowcode_api_base_url=self.settings.knowcode_api_base_url,
            timeout_seconds=self.settings.tool_timeout_seconds,
            cache_ttl_seconds=self.settings.openapi_cache_ttl_seconds,
        )
        self._knowcode = KnowCodeClient(
            base_url=self.settings.knowcode_api_base_url,
            timeout_seconds=self.settings.tool_timeout_seconds,
        )
        self._litellm = LiteLLMClient(
            base_url=self.settings.litellm_base_url,
            api_key=self.settings.litellm_api_key,
            timeout_seconds=self.settings.tool_timeout_seconds,
        )

    def list_tools(self, requested_tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if requested_tool_names is None:
            selected_names = list(dict.fromkeys(self.settings.allowed_tool_names))
        else:
            selected_names = self._pick_tool_names(
                user_message="",
                requested_tool_names=requested_tool_names,
            )
        return self._tools.get_tools(selected_names)

    def run(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.settings.default_model
        tags = list(request.tags or self.settings.default_tags)

        selected_tool_names = self._pick_tool_names(
            user_message=request.message,
            requested_tool_names=request.tool_names,
        )
        tool_schemas = self._tools.get_tools(selected_tool_names)

        messages: List[Dict[str, Any]] = [
            model_dump(message, exclude_none=True) for message in request.conversation
        ]
        messages.append({"role": "user", "content": request.message})

        usage: Dict[str, Any] = {}
        response_cost: Optional[float] = None
        finish_reason: Optional[str] = None
        tool_executions: List[ToolExecutionRecord] = []

        for _round in range(self.settings.max_tool_rounds):
            completion = self._litellm.create_chat_completion(
                model=model,
                messages=messages,
                tools=tool_schemas,
                tags=tags,
                temperature=request.temperature,
            )
            usage = completion.payload.get("usage", {})
            response_cost = completion.response_cost

            choice = self._first_choice(completion.payload)
            assistant_message = choice.get("message", {})
            if not isinstance(assistant_message, dict):
                raise LiteLLMError("Malformed LiteLLM response: missing message object")

            finish_reason_raw = choice.get("finish_reason")
            if isinstance(finish_reason_raw, str):
                finish_reason = finish_reason_raw

            assistant_content = assistant_message.get("content")
            content = assistant_content if isinstance(assistant_content, str) else ""
            tool_calls = assistant_message.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                tool_calls = []

            outgoing_assistant: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                outgoing_assistant["tool_calls"] = tool_calls
            messages.append(outgoing_assistant)

            if not tool_calls:
                return ChatResponse(
                    answer=content,
                    model=model,
                    usage=usage if isinstance(usage, dict) else {},
                    response_cost=response_cost,
                    finish_reason=finish_reason,
                    selected_tools=selected_tool_names,
                    tool_executions=tool_executions,
                )

            for tool_call in tool_calls:
                self._execute_tool_call(
                    tool_call=tool_call,
                    messages=messages,
                    tool_executions=tool_executions,
                )

        raise RuntimeError(
            f"Tool round limit reached ({self.settings.max_tool_rounds}) before final answer"
        )

    def _pick_tool_names(
        self,
        user_message: str,
        requested_tool_names: Optional[List[str]],
    ) -> List[str]:
        selected = select_tool_names(
            user_message=user_message,
            allowed_tool_names=self.settings.allowed_tool_names,
            requested_tool_names=requested_tool_names,
        )
        if not selected:
            raise ValueError("No tools selected. Check AGENT_ALLOWED_TOOL_NAMES configuration.")
        return selected

    def _first_choice(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise LiteLLMError("Malformed LiteLLM response: missing choices")

        first = choices[0]
        if not isinstance(first, dict):
            raise LiteLLMError("Malformed LiteLLM response: invalid first choice")
        return first

    def _execute_tool_call(
        self,
        *,
        tool_call: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tool_executions: List[ToolExecutionRecord],
    ) -> None:
        tool_call_id = str(tool_call.get("id") or "unknown_tool_call")
        function = tool_call.get("function", {})
        function = function if isinstance(function, dict) else {}

        tool_name = str(function.get("name") or "")
        raw_arguments = function.get("arguments", "{}")

        arguments: Dict[str, Any] = {}
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                decoded = json.loads(raw_arguments)
                if isinstance(decoded, dict):
                    arguments = decoded
            except json.JSONDecodeError:
                arguments = {}

        started = time.monotonic()
        success = True
        error: Optional[str] = None

        try:
            result = self._knowcode.execute_tool(tool_name, arguments)
        except ToolExecutionError as exc:
            success = False
            error = str(exc)
            result = {"error": error}

        latency_ms = int((time.monotonic() - started) * 1000)
        tool_executions.append(
            ToolExecutionRecord(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                success=success,
                latency_ms=latency_ms,
                error=error,
            )
        )

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result, ensure_ascii=True),
            }
        )
