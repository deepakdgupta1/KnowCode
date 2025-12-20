"""Compatibility wrapper for the moved API module."""

from knowcode.api.api import (
    ChunkResult,
    ContextResponse,
    QueryRequest,
    QueryResponse,
    SearchResult,
    get_service,
    router,
)

__all__ = [
    "ChunkResult",
    "ContextResponse",
    "QueryRequest",
    "QueryResponse",
    "SearchResult",
    "get_service",
    "router",
]
