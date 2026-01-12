"""MCP Server implementation for KnowCode.

Provides an MCP server that exposes KnowCode's codebase intelligence
to LLM applications via the Model Context Protocol.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

# MCP imports - requires: pip install mcp
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        CallToolResult,
    )
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from knowcode.service import KnowCodeService
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.data_models import TaskType


# Tool definitions for MCP
TOOL_DEFINITIONS = [
    {
        "name": "search_codebase",
        "description": "Search for code entities (functions, classes, modules) by name or pattern. Returns matching entities with their locations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search pattern (substring match on entity names)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return",
                    "default": 10
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_entity_context",
        "description": "Get detailed context for a code entity including source code, documentation, callers, and callees. Optimized for LLM consumption.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Entity ID or qualified name (e.g., 'module.ClassName.method')"
                },
                "task_type": {
                    "type": "string",
                    "enum": ["explain", "debug", "extend", "review", "locate", "general"],
                    "description": "Task type for context prioritization",
                    "default": "general"
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum tokens in context",
                    "default": 2000
                }
            },
            "required": ["entity_id"]
        }
    },
    {
        "name": "trace_calls",
        "description": "Trace the call graph from an entity. Find all callers (who calls this) or callees (what this calls) up to N hops.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Starting entity ID"
                },
                "direction": {
                    "type": "string",
                    "enum": ["callers", "callees"],
                    "description": "Direction to trace",
                    "default": "callees"
                },
                "depth": {
                    "type": "integer",
                    "description": "Maximum traversal depth (1-5)",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 5
                }
            },
            "required": ["entity_id"]
        }
    },
    {
        "name": "retrieve_context_for_query",
        "description": "Retrieve a token-budgeted, task-aware context bundle for a natural-language query. Uses the same retrieval pipeline as CLI Q&A, and returns a sufficiency_score for local-first decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to retrieve context for"
                },
                "task_type": {
                    "type": "string",
                    "enum": ["auto", "explain", "debug", "extend", "review", "locate", "general"],
                    "description": "Task type override; use 'auto' to let KnowCode classify the query",
                    "default": "auto"
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Overall token budget for returned context",
                    "default": 6000
                },
                "limit_entities": {
                    "type": "integer",
                    "description": "Maximum number of unique entities to include",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10
                },
                "expand_deps": {
                    "type": "boolean",
                    "description": "Whether to expand dependency context (call graph) around retrieved entities",
                    "default": True
                }
            },
            "required": ["query"]
        }
    }
]


class KnowCodeMCPServer:
    """MCP Server wrapper for KnowCode."""
    
    def __init__(self, store_path: str | Path, config_path: Optional[str] = None) -> None:
        """Initialize MCP server with knowledge store.
        
        Args:
            store_path: Path to knowcode_knowledge.json
            config_path: Optional path to aimodels.yaml for model priorities.
        """
        self.store_path = Path(store_path)
        self.config_path = config_path
        self._service: Optional[KnowCodeService] = None
        
    def _ensure_service(self) -> KnowCodeService:
        """Create the shared service (loads store/index lazily)."""
        if self._service is None:
            store_file = self.store_path
            if store_file.is_dir():
                store_file = store_file / KnowledgeStore.DEFAULT_FILENAME

            if not store_file.exists():
                raise FileNotFoundError(
                    f"Knowledge store not found: {store_file}\n"
                    "Run 'knowcode analyze' first to build the knowledge graph."
                )

            self._service = KnowCodeService(
                store_path=self.store_path,
                config_path=self.config_path,
            )
        return self._service
    
    def search_codebase(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for entities by name pattern.
        
        Args:
            query: Search pattern.
            limit: Maximum results.
            
        Returns:
            List of matching entity summaries.
        """
        service = self._ensure_service()
        entities = service.store.search(query)[:limit]
        
        return [
            {
                "id": e.id,
                "name": e.name,
                "qualified_name": e.qualified_name,
                "kind": e.kind.value,
                "file": e.location.file_path,
                "line": e.location.line_start,
            }
            for e in entities
        ]
    
    def get_entity_context(
        self,
        entity_id: str,
        task_type: str = "general",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Get synthesized context for an entity.
        
        Args:
            entity_id: Entity ID or search pattern.
            task_type: Task type for prioritization.
            max_tokens: Token budget.
            
        Returns:
            Context bundle with sufficiency score.
        """
        service = self._ensure_service()
        try:
            task = TaskType(task_type)
        except ValueError:
            task = TaskType.GENERAL

        try:
            bundle = service.get_context(entity_id, max_tokens=max_tokens, task_type=task)
        except ValueError:
            return {
                "error": f"Entity not found: {entity_id}",
                "context_text": "",
                "sufficiency_score": 0.0,
                "task_type": task.value,
            }

        entity = service.store.get_entity(bundle.get("entity_id", entity_id))
        qualified_name = entity.qualified_name if entity else ""

        return {
            "entity_id": bundle.get("entity_id", entity_id),
            "qualified_name": qualified_name,
            "context_text": bundle.get("context_text", ""),
            "total_tokens": bundle.get("total_tokens", 0),
            "sufficiency_score": bundle.get("sufficiency_score", 0.0),
            "task_type": bundle.get("task_type", task.value),
        }
    
    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """Trace call graph from an entity.
        
        Args:
            entity_id: Starting entity.
            direction: "callers" or "callees".
            depth: Max hops.
            
        Returns:
            List of entities with call_depth.
        """
        service = self._ensure_service()
        return service.store.trace_calls(
            entity_id,
            direction=direction,
            depth=min(depth, 5),
            max_results=50,
        )

    def retrieve_context_for_query(
        self,
        query: str,
        task_type: str = "auto",
        max_tokens: int = 6000,
        limit_entities: int = 3,
        expand_deps: bool = True,
    ) -> dict[str, Any]:
        """Retrieve a task-aware context bundle for a query."""
        service = self._ensure_service()
        task_override: Optional[TaskType] = None
        if task_type != "auto":
            try:
                task_override = TaskType(task_type)
            except ValueError:
                task_override = TaskType.GENERAL

        return service.retrieve_context_for_query(
            query=query,
            max_tokens=max_tokens,
            task_type=task_override,
            limit_entities=limit_entities,
            expand_deps=expand_deps,
        )
    
    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> str:
        """Handle an MCP tool call.
        
        Args:
            name: Tool name.
            arguments: Tool arguments.
            
        Returns:
            JSON string result.
        """
        try:
            if name == "search_codebase":
                result = self.search_codebase(
                    query=arguments["query"],
                    limit=arguments.get("limit", 10),
                )
            elif name == "get_entity_context":
                result = self.get_entity_context(
                    entity_id=arguments["entity_id"],
                    task_type=arguments.get("task_type", "general"),
                    max_tokens=arguments.get("max_tokens", 2000),
                )
            elif name == "trace_calls":
                result = self.trace_calls(
                    entity_id=arguments["entity_id"],
                    direction=arguments.get("direction", "callees"),
                    depth=arguments.get("depth", 1),
                )
            elif name == "retrieve_context_for_query":
                result = self.retrieve_context_for_query(
                    query=arguments["query"],
                    task_type=arguments.get("task_type", "auto"),
                    max_tokens=arguments.get("max_tokens", 6000),
                    limit_entities=arguments.get("limit_entities", 3),
                    expand_deps=arguments.get("expand_deps", True),
                )
            else:
                result = {"error": f"Unknown tool: {name}"}
                
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})


def create_server(store_path: str | Path, config_path: Optional[str] = None) -> "Server":
    """Create an MCP server instance.
    
    Args:
        store_path: Path to knowledge store.
        config_path: Optional configuration file path for model priorities.
        
    Returns:
        Configured MCP Server.
    """
    if not MCP_AVAILABLE:
        raise ImportError(
            "MCP package not installed. Install with: pip install mcp"
        )
    
    server = Server("knowcode")
    knowcode = KnowCodeMCPServer(store_path, config_path=config_path)
    
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available KnowCode tools."""
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOL_DEFINITIONS
        ]
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a KnowCode tool."""
        result_text = knowcode.handle_tool_call(name, arguments)
        return CallToolResult(
            content=[TextContent(type="text", text=result_text)]
        )
    
    return server


async def run_server_async(store_path: str | Path, config_path: Optional[str] = None) -> None:
    """Run the MCP server with STDIO transport.
    
    Args:
        store_path: Path to knowledge store.
        config_path: Optional configuration file path for model priorities.
    """
    if not MCP_AVAILABLE:
        raise ImportError(
            "MCP package not installed. Install with: pip install mcp"
        )
    
    server = create_server(store_path, config_path=config_path)
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_server(store_path: str | Path, config_path: Optional[str] = None) -> None:
    """Run the MCP server (blocking).
    
    Args:
        store_path: Path to knowledge store.
        config_path: Optional configuration file path for model priorities.
    """
    asyncio.run(run_server_async(store_path, config_path=config_path))
