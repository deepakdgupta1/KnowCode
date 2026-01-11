"""FastAPI endpoints for KnowCode."""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any, Optional, Literal
from pydantic import BaseModel
from enum import Enum

from knowcode.service import KnowCodeService
from knowcode.data_models import TaskType

router = APIRouter(prefix="/api/v1")

# Global service instance (will be initialized by main.py)
_service: Optional[KnowCodeService] = None

def get_service() -> KnowCodeService:
    """Return the global service instance or raise if uninitialized."""
    if _service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _service


class TaskTypeParam(str, Enum):
    """Task type for context prioritization."""
    explain = "explain"
    debug = "debug"
    extend = "extend"
    review = "review"
    locate = "locate"
    general = "general"


class SearchResult(BaseModel):
    """Response model for entity search results."""
    id: str
    kind: str
    name: str
    qualified_name: str
    file: str
    line: int

class ContextResponse(BaseModel):
    """Response model for synthesized entity context."""
    entity_id: str
    context_text: str
    total_tokens: int
    truncated: bool
    included_entities: list[str]
    task_type: str = "general"
    sufficiency_score: float = 0.0

class ChunkResult(BaseModel):
    """Response model for a retrieved chunk."""
    id: str
    content: str
    entity_id: str
    score: float

class QueryRequest(BaseModel):
    """Request model for semantic search queries."""
    query: str
    limit: Optional[int] = 5
    expand_deps: Optional[bool] = True
    task_type: Optional[TaskTypeParam] = TaskTypeParam.general

class QueryResponse(BaseModel):
    """Response model for semantic search queries."""
    chunks: list[ChunkResult]
    total: int
    task_type: str = "general"

@router.get("/health", summary="Health Check")
def health() -> dict[str, str]:
    """Check if the server is running and reachable."""
    return {"status": "ok"}

@router.get("/stats", summary="Get Knowledge Graph Stats")
def get_stats(service: KnowCodeService = Depends(get_service)) -> dict[str, Any]:
    """Returns statistics about the number of entities and relationships in the graph."""
    return service.get_stats()

@router.post("/context/query", response_model=QueryResponse, summary="Query Codebase Semantically")
def query_context(
    request: QueryRequest,
    service: KnowCodeService = Depends(get_service)
) -> QueryResponse:
    """Execute semantic search and return relevant code chunks with context."""
    from knowcode.retrieval.search_engine import SearchEngine
    
    engine = service.get_search_engine()
    chunks = engine.search(
        query=request.query,
        limit=request.limit or 5,
        expand_deps=request.expand_deps if request.expand_deps is not None else True
    )
    
    results = [
        ChunkResult(
            id=c.id,
            content=c.content,
            entity_id=c.entity_id,
            score=0.0 # Score not easily exposed from engine currently
        )
        for c in chunks
    ]
    
    return QueryResponse(
        chunks=results,
        total=len(results),
        task_type=request.task_type.value if request.task_type else "general"
    )

@router.get("/search", response_model=list[SearchResult], summary="Search Entities")
def search(
    q: str = Query(..., min_length=1, description="Search pattern (substring match on name or qualified name)"),
    service: KnowCodeService = Depends(get_service)
) -> list[Any]:
    """Search for entities matching the given query string."""
    return service.search(q)

@router.get("/context", response_model=ContextResponse, summary="Get Entity Context")
def get_context(
    target: str = Query(..., min_length=1, description="Entity ID or name to get context for"),
    max_tokens: int = Query(2000, description="Maximum amount of tokens allowed in the returned context"),
    task_type: TaskTypeParam = Query(TaskTypeParam.general, description="Task type for context prioritization"),
    service: KnowCodeService = Depends(get_service)
) -> Any:
    """Generates a synthesized context bundle for an entity, optimized for LLM consumption.
    
    The task_type parameter enables task-specific context prioritization:
    - explain: Prioritizes docstrings, signatures, and callees for understanding
    - debug: Prioritizes source code and callers for tracing issues
    - extend: Prioritizes patterns, children, and signatures for adding code
    - review: Prioritizes changes, callers, and callees for impact analysis
    - locate: Minimal context, just location info
    - general: Balanced context (default)
    
    Returns sufficiency_score (0.0-1.0) indicating if context is sufficient for local answering.
    """
    try:
        # Convert API enum to data model enum
        data_task_type = TaskType(task_type.value)
        return service.get_context(target, max_tokens=max_tokens, task_type=data_task_type)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/reload", summary="Reload Knowledge Store")
def reload_store(service: KnowCodeService = Depends(get_service)) -> dict[str, str]:
    """Reload the knowledge store from disk."""
    try:
        service.reload()
        return {"status": "reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/entities/{entity_id:path}")
def get_entity(
    entity_id: str,
    service: KnowCodeService = Depends(get_service)
) -> Any:
    """Get raw entity details."""
    details = service.get_entity_details(entity_id)
    if not details:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
    return details

@router.get("/callers/{entity_id:path}", summary="Get Entity Callers")
def get_callers(
    entity_id: str,
    service: KnowCodeService = Depends(get_service)
) -> list[Any]:
    """Find all entities that call the specified entity."""
    return service.get_callers(entity_id)

@router.get("/callees/{entity_id:path}", summary="Get Entity Callees")
def get_callees(
    entity_id: str,
    service: KnowCodeService = Depends(get_service)
) -> list[Any]:
    """Find all entities called by the specified entity."""
    return service.get_callees(entity_id)


class DirectionParam(str, Enum):
    """Direction for call graph traversal."""
    callers = "callers"
    callees = "callees"


@router.get("/trace_calls/{entity_id:path}", summary="Multi-hop Call Trace")
def trace_calls(
    entity_id: str,
    direction: DirectionParam = Query(DirectionParam.callees, description="Direction: callers or callees"),
    depth: int = Query(1, ge=1, le=5, description="Traversal depth (1-5)"),
    max_results: int = Query(50, ge=1, le=100, description="Max results"),
    service: KnowCodeService = Depends(get_service)
) -> list[dict[str, Any]]:
    """Multi-hop call graph traversal.
    
    Traverse the call graph from a starting entity to find all callers or callees
    up to the specified depth. Each result includes the call_depth indicating
    how many hops from the starting entity.
    """
    return service.store.trace_calls(
        entity_id,
        direction=direction.value,
        depth=depth,
        max_results=max_results
    )


@router.get("/impact/{entity_id:path}", summary="Impact Analysis")
def get_impact(
    entity_id: str,
    max_depth: int = Query(3, ge=1, le=5, description="Max depth for transitive analysis"),
    service: KnowCodeService = Depends(get_service)
) -> dict[str, Any]:
    """Analyze the impact of modifying or deleting an entity.
    
    Returns:
    - direct_dependents: Entities that directly depend on this entity
    - transitive_dependents: Entities affected through the dependency chain
    - affected_files: Files that would need review
    - risk_score: 0.0-1.0 indicating modification risk
    """
    return service.store.get_impact(entity_id, max_depth=max_depth)
