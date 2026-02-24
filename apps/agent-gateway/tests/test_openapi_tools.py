"""Unit tests for OpenAPI -> tool translation."""

from __future__ import annotations

from typing import Any, Dict

from agent_gateway.openapi_tools import OpenAPIToolTranslator



def _sample_spec() -> Dict[str, Any]:
    return {
        "openapi": "3.0.0",
        "paths": {
            "/api/v1/context/query": {
                "post": {
                    "operationId": "query_context",
                    "summary": "Query context for semantic retrieval",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "nullable": True},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/QueryRequest"}
                            }
                        },
                    },
                }
            },
            "/api/v1/search": {
                "get": {
                    "operationId": "search",
                    "summary": "Search symbols",
                    "parameters": [
                        {
                            "name": "q",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                }
            },
        },
        "components": {
            "schemas": {
                "QueryRequest": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "title": "Query",
                            "description": "Search query",
                        },
                        "task_type": {
                            "type": "string",
                            "enum": ["general", "debug"],
                        },
                    },
                    "required": ["query"],
                }
            }
        },
    }



def test_translate_filters_to_allowed_operations() -> None:
    translator = OpenAPIToolTranslator()
    tools = translator.translate(_sample_spec(), allowed_operation_ids=["search"])

    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "search"



def test_translate_merges_parameters_and_strips_openapi_noise() -> None:
    translator = OpenAPIToolTranslator()
    tools = translator.translate(_sample_spec(), allowed_operation_ids=["query_context"])

    assert len(tools) == 1
    function = tools[0]["function"]
    parameters = function["parameters"]
    properties = parameters["properties"]

    assert "query" in properties
    assert "task_type" in properties
    assert "limit" in properties
    assert "title" not in properties["query"]
    assert "query" in parameters["required"]

    limit_type = properties["limit"].get("type")
    assert isinstance(limit_type, list)
    assert set(limit_type) == {"integer", "null"}


def test_translate_maps_fastapi_generated_operation_ids_to_canonical_names() -> None:
    spec = _sample_spec()
    spec["paths"]["/api/v1/context/query"]["post"]["operationId"] = (
        "query_context_api_v1_context_query_post"
    )

    translator = OpenAPIToolTranslator()
    tools = translator.translate(spec, allowed_operation_ids=["query_context"])

    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "query_context"
