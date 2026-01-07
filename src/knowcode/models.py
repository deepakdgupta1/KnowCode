"""Compatibility module for legacy imports.

Re-exports data model symbols from knowcode.data_models.
"""

from knowcode.data_models import (  # noqa: F401
    ChunkingConfig,
    CodeChunk,
    EmbeddingConfig,
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)

