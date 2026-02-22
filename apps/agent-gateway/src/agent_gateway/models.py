"""Shared request/response models for the gateway API."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """One chat message in OpenAI-compatible format."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ChatRequest(BaseModel):
    """Request payload for running the agent loop."""

    message: str = Field(..., min_length=1)
    conversation: List[ChatMessage] = Field(default_factory=list)
    model: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    tool_names: Optional[List[str]] = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class ToolExecutionRecord(BaseModel):
    """Execution metadata for one tool call."""

    tool_name: str
    tool_call_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    success: bool
    latency_ms: int
    error: Optional[str] = None


class ChatResponse(BaseModel):
    """Gateway response with final answer and execution telemetry."""

    answer: str
    model: str
    usage: Dict[str, Any] = Field(default_factory=dict)
    response_cost: Optional[float] = None
    finish_reason: Optional[str] = None
    selected_tools: List[str] = Field(default_factory=list)
    tool_executions: List[ToolExecutionRecord] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Standard API error response."""

    error: str
    detail: Optional[str] = None



def model_dump(model: BaseModel, **kwargs: Any) -> Dict[str, Any]:
    """Compat dump for Pydantic v1/v2."""

    if hasattr(model, "model_dump"):
        return getattr(model, "model_dump")(**kwargs)
    return model.dict(**kwargs)
