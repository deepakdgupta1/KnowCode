"""FastAPI endpoints for KnowCode."""

from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.requests import Request

from knowcode.service import KnowCodeService
from knowcode.data_models import TaskType
from knowcode.api.rate_limit import limiter, STANDARD_LIMIT, EXPENSIVE_LIMIT
from knowcode.errors import KnowCodePrerequisiteError

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
    limit: Optional[int] = Field(
        default=5, le=20, description="Max results (capped at 20)"
    )
    max_tokens: Optional[int] = Field(
        default=4000, le=8000, description="Max total tokens in response content"
    )
    expand_deps: Optional[bool] = True
    task_type: Optional[TaskTypeParam] = TaskTypeParam.general


class QueryResponse(BaseModel):
    """Response model for semantic search queries."""

    chunks: list[ChunkResult]
    total: int
    task_type: str = "general"


class FreshnessResponse(BaseModel):
    """Response model for codebase freshness check."""

    last_store_rebuild: int
    last_index_rebuild: int
    latest_source_change: int
    is_stale: bool
    stale_reasons: list[str]


@router.get("/health", summary="Health Check")
@limiter.limit(STANDARD_LIMIT)
def health(request: Request) -> dict[str, str]:
    """Check if the server is running and reachable."""
    return {"status": "ok"}


@router.get("/stats", summary="Get Knowledge Graph Stats")
@limiter.limit(STANDARD_LIMIT)
def get_stats(
    request: Request, service: KnowCodeService = Depends(get_service)
) -> dict[str, Any]:
    """Returns statistics about the number of entities and relationships in the graph."""
    return service.get_stats()


@router.post(
    "/context/query",
    response_model=QueryResponse,
    summary="Query Codebase Semantically",
)
@limiter.limit(STANDARD_LIMIT)
def query_context(
    request: Request,
    payload: QueryRequest,
    service: KnowCodeService = Depends(get_service),
) -> QueryResponse:
    """Execute task-aware retrieval and return relevant code chunks with scores.

    The Starlette ``request`` is first and named ``request`` because SlowAPI's
    ``@limiter.limit`` decorator keys on the parameter with that exact name and
    asserts it is a ``starlette.requests.Request``. The JSON body is a separate
    ``payload`` parameter resolved by type, so the request contract is unchanged.
    """
    _ = request  # consumed by the limiter decorator, not the handler body
    limit = min(max(1, payload.limit or 5), 20)
    max_tokens_budget = min(payload.max_tokens or 4000, 8000)
    expand_deps = payload.expand_deps if payload.expand_deps is not None else True
    task_override = TaskType(payload.task_type.value) if payload.task_type else None

    # One generation for the whole request (Step 18). Retrieval names the
    # chunk ids and the repository resolves them; a rebuild landing between
    # those two steps would otherwise resolve one generation's ids against
    # the next generation's chunks and silently drop the misses.
    with service.generation_lease():
        try:
            retrieval = service.retrieve_context_for_query(
                query=payload.query,
                task_type=task_override,
                limit_entities=limit,
                expand_deps=expand_deps,
            )
        except KnowCodePrerequisiteError as e:
            raise HTTPException(
                status_code=412,
                detail={"message": str(e), "code": e.code, "hint": e.hint},
            )

        engine = service.get_search_engine()
        results: list[ChunkResult] = []
        seen_ids: set[str] = set()

        evidence_items = retrieval.get("evidence", [])
        if isinstance(evidence_items, list):
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue

                chunk_id = item.get("chunk_id")
                if not isinstance(chunk_id, str) or chunk_id in seen_ids:
                    continue

                stored_chunk = engine.chunk_repo.get(chunk_id)
                if not stored_chunk:
                    continue

                score_raw = item.get("score", 0.0)
                score = float(score_raw) if isinstance(score_raw, (int, float)) else 0.0
                results.append(
                    ChunkResult(
                        id=stored_chunk.id,
                        content=stored_chunk.content,
                        entity_id=stored_chunk.entity_id,
                        score=score,
                    )
                )
                seen_ids.add(chunk_id)
                if len(results) >= limit:
                    break

        # Fallback for lexical-only retrieval paths where no chunk IDs are available.
        if not results:
            fallback_chunks = engine.search(
                query=payload.query,
                limit=limit,
                expand_deps=expand_deps,
            )
            results = [
                ChunkResult(
                    id=c.id,
                    content=c.content,
                    entity_id=c.entity_id,
                    score=0.0,
                )
                for c in fallback_chunks
            ]

    # --- Response capping: truncate chunk content to stay within token budget ---
    total_chars = 0
    capped_results: list[ChunkResult] = []
    for result_chunk in results:
        if total_chars + len(result_chunk.content) > max_tokens_budget:
            remaining = max_tokens_budget - total_chars
            if remaining > 100:  # Only include if meaningful content fits
                capped_results.append(
                    ChunkResult(
                        id=result_chunk.id,
                        content=result_chunk.content[:remaining] + "\n... [TRUNCATED]",
                        entity_id=result_chunk.entity_id,
                        score=result_chunk.score,
                    )
                )
            break
        capped_results.append(result_chunk)
        total_chars += len(result_chunk.content)

    return QueryResponse(
        chunks=capped_results,
        total=len(capped_results),
        task_type=str(
            retrieval.get(
                "task_type", payload.task_type.value if payload.task_type else "general"
            )
        ),
    )


@router.get("/search", response_model=list[SearchResult], summary="Search Entities")
@limiter.limit(STANDARD_LIMIT)
def search(
    request: Request,
    q: str = Query(
        ...,
        min_length=1,
        description="Search pattern (substring match on name or qualified name)",
    ),
    limit: int = Query(20, ge=1, le=50, description="Max results (default 20, max 50)"),
    service: KnowCodeService = Depends(get_service),
) -> list[Any]:
    """Search for entities matching the given query string."""
    results = service.search(q)
    return results[:limit]


@router.get("/context", response_model=ContextResponse, summary="Get Entity Context")
@limiter.limit(STANDARD_LIMIT)
def get_context(
    request: Request,
    target: str = Query(
        ..., min_length=1, description="Entity ID or name to get context for"
    ),
    max_tokens: int = Query(
        2000,
        ge=100,
        le=8000,
        description="Maximum tokens in returned context (100-8000)",
    ),
    task_type: TaskTypeParam = Query(
        TaskTypeParam.general, description="Task type for context prioritization"
    ),
    service: KnowCodeService = Depends(get_service),
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
        return service.get_context(
            target, max_tokens=max_tokens, task_type=data_task_type
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/reload", summary="Reload Knowledge Store")
@limiter.limit(STANDARD_LIMIT)
def reload_store(
    request: Request, service: KnowCodeService = Depends(get_service)
) -> dict[str, str]:
    """Reload the knowledge store from disk."""
    try:
        service.reload()
        return {"status": "reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/freshness", response_model=FreshnessResponse, summary="Get Codebase Freshness"
)
@limiter.limit(STANDARD_LIMIT)
def get_freshness(
    request: Request, service: KnowCodeService = Depends(get_service)
) -> FreshnessResponse:
    """Check freshness status of the knowledge store and semantic index."""
    meta = service.get_freshness_metadata()
    return FreshnessResponse(**meta)


@router.get("/entities/{entity_id:path}")
@limiter.limit(STANDARD_LIMIT)
def get_entity(
    request: Request, entity_id: str, service: KnowCodeService = Depends(get_service)
) -> Any:
    """Get raw entity details."""
    details = service.get_entity_details(entity_id)
    if not details:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
    return details


@router.get("/callers/{entity_id:path}", summary="Get Entity Callers")
@limiter.limit(STANDARD_LIMIT)
def get_callers(
    request: Request, entity_id: str, service: KnowCodeService = Depends(get_service)
) -> list[Any]:
    """Find all entities that call the specified entity."""
    return service.get_callers(entity_id)


@router.get("/callees/{entity_id:path}", summary="Get Entity Callees")
@limiter.limit(STANDARD_LIMIT)
def get_callees(
    request: Request, entity_id: str, service: KnowCodeService = Depends(get_service)
) -> list[Any]:
    """Find all entities called by the specified entity."""
    return service.get_callees(entity_id)


class DirectionParam(str, Enum):
    """Direction for call graph traversal."""

    callers = "callers"
    callees = "callees"


@router.get("/trace_calls/{entity_id:path}", summary="Multi-hop Call Trace")
@limiter.limit(EXPENSIVE_LIMIT)
def trace_calls(
    request: Request,
    entity_id: str,
    direction: DirectionParam = Query(
        DirectionParam.callees, description="Direction: callers or callees"
    ),
    depth: int = Query(1, ge=1, le=5, description="Traversal depth (1-5)"),
    max_results: int = Query(50, ge=1, le=100, description="Max results"),
    service: KnowCodeService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Multi-hop call graph traversal.

    Traverse the call graph from a starting entity to find all callers or callees
    up to the specified depth. Each result includes the call_depth indicating
    how many hops from the starting entity.
    """
    from typing import cast

    return cast(
        list[dict[str, Any]],
        service.store.trace_calls(
            entity_id, direction=direction.value, depth=depth, max_results=max_results
        ),
    )


@router.get("/impact/{entity_id:path}", summary="Impact Analysis")
@limiter.limit(EXPENSIVE_LIMIT)
def get_impact(
    request: Request,
    entity_id: str,
    max_depth: int = Query(
        3, ge=1, le=5, description="Max depth for transitive analysis"
    ),
    service: KnowCodeService = Depends(get_service),
) -> dict[str, Any]:
    """Analyze the impact of modifying or deleting an entity.

    Returns:
    - direct_dependents: Entities that directly depend on this entity
    - transitive_dependents: Entities affected through the dependency chain
    - affected_files: Files that would need review
    - risk_score: 0.0-1.0 indicating modification risk
    """
    from typing import cast

    return cast(
        dict[str, Any], service.store.get_impact(entity_id, max_depth=max_depth)
    )
