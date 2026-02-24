"""FastAPI application for the extractable KnowCode agent gateway."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request

from agent_gateway.litellm_client import LiteLLMError
from agent_gateway.models import ChatRequest, ChatResponse, ErrorResponse
from agent_gateway.openapi_tools import OpenAPIFetchError, OpenAPITranslationError
from agent_gateway.orchestrator import AgentOrchestrator
from agent_gateway.settings import GatewaySettings

logger = logging.getLogger("agent_gateway")


def create_app() -> FastAPI:
    """Create and wire the gateway API application."""

    settings = GatewaySettings.from_env()
    settings.validate()
    orchestrator = AgentOrchestrator(settings=settings)

    app = FastAPI(
        title="KnowCode Agent Gateway",
        description="LiteLLM-backed tool execution gateway for KnowCode",
        version="0.1.0",
    )

    @app.middleware("http")
    async def attach_request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip() or str(uuid.uuid4())
        request.state.request_id = request_id

        started = time.monotonic()
        response = await call_next(request)
        latency_ms = int((time.monotonic() - started) * 1000)

        response.headers["x-request-id"] = request_id
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                },
                ensure_ascii=True,
            )
        )
        return response

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> Dict[str, Any]:
        report = orchestrator.readiness()
        if report.get("status") != "ok":
            raise HTTPException(status_code=503, detail=report)
        return report

    @app.get("/api/v1/config")
    def config_snapshot() -> Dict[str, Any]:
        return {
            "knowcode_api_base_url": settings.knowcode_api_base_url,
            "litellm_base_url": settings.litellm_base_url,
            "default_model": settings.default_model,
            "max_tool_rounds": settings.max_tool_rounds,
            "allowed_tool_names": list(settings.allowed_tool_names),
            "default_tags": list(settings.default_tags),
        }

    @app.get("/api/v1/tools")
    def list_tools(
        names: Optional[List[str]] = Query(
            default=None,
            description="Optional repeated query arg to filter tool names",
        )
    ) -> Dict[str, Any]:
        try:
            tools = orchestrator.list_tools(requested_tool_names=names)
        except (OpenAPIFetchError, OpenAPITranslationError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"tools": tools, "count": len(tools)}

    @app.post(
        "/api/v1/chat",
        response_model=ChatResponse,
        responses={
            400: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
    )
    def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        started = time.monotonic()

        try:
            response = orchestrator.run(payload)
            total_tool_latency_ms = sum(item.latency_ms for item in response.tool_executions)
            failed_tool_calls = sum(1 for item in response.tool_executions if not item.success)
            logger.info(
                json.dumps(
                    {
                        "event": "chat_completed",
                        "request_id": request_id,
                        "model": response.model,
                        "finish_reason": response.finish_reason,
                        "selected_tools": response.selected_tools,
                        "tool_call_count": len(response.tool_executions),
                        "failed_tool_calls": failed_tool_calls,
                        "total_tool_latency_ms": total_tool_latency_ms,
                        "response_cost": response.response_cost,
                        "usage": response.usage,
                        "request_latency_ms": int((time.monotonic() - started) * 1000),
                        "message_length": len(payload.message),
                    },
                    ensure_ascii=True,
                )
            )
            return response
        except ValueError as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "chat_failed",
                        "request_id": request_id,
                        "error_type": "validation",
                        "error": str(exc),
                    },
                    ensure_ascii=True,
                )
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OpenAPIFetchError, OpenAPITranslationError, LiteLLMError, RuntimeError) as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "chat_failed",
                        "request_id": request_id,
                        "error_type": "upstream",
                        "error": str(exc),
                    },
                    ensure_ascii=True,
                )
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "Unhandled gateway exception request_id=%s",
                request_id,
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app
