"""FastAPI application for the extractable KnowCode agent gateway."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

from agent_gateway.litellm_client import LiteLLMError
from agent_gateway.models import ChatRequest, ChatResponse, ErrorResponse
from agent_gateway.openapi_tools import OpenAPIFetchError, OpenAPITranslationError
from agent_gateway.orchestrator import AgentOrchestrator
from agent_gateway.settings import GatewaySettings



def create_app() -> FastAPI:
    """Create and wire the gateway API application."""

    settings = GatewaySettings.from_env()
    orchestrator = AgentOrchestrator(settings=settings)

    app = FastAPI(
        title="KnowCode Agent Gateway",
        description="LiteLLM-backed tool execution gateway for KnowCode",
        version="0.1.0",
    )

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

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
    def chat(request: ChatRequest) -> ChatResponse:
        try:
            return orchestrator.run(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OpenAPIFetchError, OpenAPITranslationError, LiteLLMError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app
