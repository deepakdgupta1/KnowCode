"""MCP Server subpackage for KnowCode.

Exposes KnowCode tools to LLM applications via the Model Context Protocol (MCP).
Uses STDIO transport for local IDE integration.
"""

from knowcode.mcp.server import create_server, run_server

__all__ = ["create_server", "run_server"]
