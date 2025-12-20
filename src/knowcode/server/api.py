"""FastAPI endpoints for KnowCode."""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any, Optional
from pydantic import BaseModel

from knowcode.service import KnowCodeService

router = APIRouter(prefix="/api/v1")

# Global service instance (will be initialized by main.py)
_service: Optional[KnowCodeService] = None

def get_service() -> KnowCodeService:
    """Return the global service instance or raise if uninitialized."""
    if _service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _service

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

class QueryResponse(BaseModel):
    """Response model for semantic search queries."""
    chunks: list[ChunkResult]
    total: int

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
    from knowcode.search_engine import SearchEngine
    
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
    
    return QueryResponse(chunks=results, total=len(results))

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
    service: KnowCodeService = Depends(get_service)
) -> Any:
    """Generates a synthesized context bundle for an entity, optimized for LLM consumption."""
    try:
        return service.get_context(target, max_tokens=max_tokens)
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
