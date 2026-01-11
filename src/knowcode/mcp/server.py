"""MCP Server implementation for KnowCode.

Provides an MCP server that exposes KnowCode's codebase intelligence
to LLM applications via the Model Context Protocol.
"""

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

from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.analysis.context_synthesizer import ContextSynthesizer
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
    }
]


class KnowCodeMCPServer:
    """MCP Server wrapper for KnowCode."""
    
    def __init__(self, store_path: str | Path) -> None:
        """Initialize MCP server with knowledge store.
        
        Args:
            store_path: Path to knowcode_knowledge.json
        """
        self.store_path = Path(store_path)
        self.store: Optional[KnowledgeStore] = None
        
    def _ensure_store(self) -> KnowledgeStore:
        """Load knowledge store if not already loaded."""
        if self.store is None:
            if not self.store_path.exists():
                raise FileNotFoundError(
                    f"Knowledge store not found: {self.store_path}\n"
                    "Run 'knowcode analyze' first to build the knowledge graph."
                )
            self.store = KnowledgeStore.load(self.store_path)
        return self.store
    
    def search_codebase(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for entities by name pattern.
        
        Args:
            query: Search pattern.
            limit: Maximum results.
            
        Returns:
            List of matching entity summaries.
        """
        store = self._ensure_store()
        entities = store.search(query)[:limit]
        
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
        store = self._ensure_store()
        
        # Find entity (exact match or search)
        entity = store.get_entity(entity_id)
        if not entity:
            matches = store.search(entity_id)
            if matches:
                entity = matches[0]
        
        if not entity:
            return {
                "error": f"Entity not found: {entity_id}",
                "context_text": "",
                "sufficiency_score": 0.0,
            }
        
        # Synthesize context
        synthesizer = ContextSynthesizer(store, max_tokens=max_tokens)
        
        try:
            task = TaskType(task_type)
        except ValueError:
            task = TaskType.GENERAL
            
        bundle = synthesizer.synthesize_with_task(entity.id, task)
        
        if not bundle:
            return {
                "error": "Failed to synthesize context",
                "context_text": "",
                "sufficiency_score": 0.0,
            }
        
        return {
            "entity_id": bundle.target_entity.id,
            "qualified_name": bundle.target_entity.qualified_name,
            "context_text": bundle.context_text,
            "total_tokens": bundle.total_tokens,
            "sufficiency_score": bundle.sufficiency_score,
            "task_type": task.value,
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
        store = self._ensure_store()
        return store.trace_calls(
            entity_id,
            direction=direction,
            depth=min(depth, 5),
            max_results=50,
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
            else:
                result = {"error": f"Unknown tool: {name}"}
                
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})


def create_server(store_path: str | Path) -> "Server":
    """Create an MCP server instance.
    
    Args:
        store_path: Path to knowledge store.
        
    Returns:
        Configured MCP Server.
    """
    if not MCP_AVAILABLE:
        raise ImportError(
            "MCP package not installed. Install with: pip install mcp"
        )
    
    server = Server("knowcode")
    knowcode = KnowCodeMCPServer(store_path)
    
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


async def run_server_async(store_path: str | Path) -> None:
    """Run the MCP server with STDIO transport.
    
    Args:
        store_path: Path to knowledge store.
    """
    if not MCP_AVAILABLE:
        raise ImportError(
            "MCP package not installed. Install with: pip install mcp"
        )
    
    server = create_server(store_path)
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_server(store_path: str | Path) -> None:
    """Run the MCP server (blocking).
    
    Args:
        store_path: Path to knowledge store.
    """
    asyncio.run(run_server_async(store_path))
