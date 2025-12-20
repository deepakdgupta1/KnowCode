"""Dependency-aware completeness expansion for code context."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowcode.chunk_repository import ChunkRepository
    from knowcode.knowledge_store import KnowledgeStore
    from knowcode.models import CodeChunk


def expand_dependencies(
    chunk: CodeChunk,
    chunk_repo: ChunkRepository,
    knowledge_store: KnowledgeStore,
    max_depth: int = 1
) -> list[CodeChunk]:
    """Expand a chunk to include dependency context.
    Uses knowledge graph to find related entities,
    then retrieves their chunks.

    Args:
        chunk: Starting chunk whose dependencies should be expanded.
        chunk_repo: Repository used to fetch chunks by entity.
        knowledge_store: Graph store used to resolve dependencies.
        max_depth: Depth of dependency expansion (1 = direct callees only).

    Returns:
        List of chunks including the input chunk and its dependencies.
    """
    expanded: list[CodeChunk] = [chunk]
    visited: set[str] = {chunk.entity_id}
    
    to_expand = [chunk.entity_id]
    depth = 0
    
    while to_expand and depth < max_depth:
        next_level = []
        for entity_id in to_expand:
            # Get callees from graph
            callees = knowledge_store.get_callees(entity_id)
            for callee in callees:
                if callee.id not in visited:
                    visited.add(callee.id)
                    next_level.append(callee.id)
                    # Get chunks for this entity
                    entity_chunks = chunk_repo.get_by_entity(callee.id)
                    expanded.extend(entity_chunks)
        
        to_expand = next_level
        depth += 1
    
    return expanded
