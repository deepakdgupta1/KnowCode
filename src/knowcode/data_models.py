"""Data models for KnowCode entities and relationships."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EntityKind(str, Enum):
    """Types of code entities tracked by the system.
    
    These correspond to semantic nodes in the knowledge graph.
    """

    MODULE = "module"          # Python modules, Java packages
    CLASS = "class"            # Classes, Interfaces, Enums
    FUNCTION = "function"      # Top-level functions
    METHOD = "method"          # Class methods
    VARIABLE = "variable"      # (Future) Top-level variables/constants
    # Documentation entities
    DOCUMENT = "document"      # Markdown files
    SECTION = "section"        # Headings within documents
    # Configuration entities
    CONFIG_KEY = "config_key"  # YAML/JSON keys
    # Temporal entities
    COMMIT = "commit"          # Git commits
    AUTHOR = "author"          # Git authors
    # Runtime entities
    TEST_RUN = "test_run"      # Test execution result
    COVERAGE_REPORT = "coverage_report"


class RelationshipKind(str, Enum):
    """Types of relationships (edges) between entities."""

    CALLS = "calls"            # Static function/method call
    IMPORTS = "imports"        # Module import / dependency
    CONTAINS = "contains"      # Structural containment (Class -> Method)
    INHERITS = "inherits"      # Class inheritance / Interface implementation
    REFERENCES = "references"  # General reference (e.g., config usage)
    # Temporal relationships
    CHANGED_BY = "changed_by"  # Entity -> Commit
    AUTHORED = "authored"      # Author -> Commit
    MODIFIED = "modified"      # Commit -> Entity
    # Runtime relationships
    COVERS = "covers"           # Report/Test -> Entity
    EXECUTED_BY = "executed_by" # Entity -> Report/Test

@dataclass
class Location:
    """Source location of an entity."""

    file_path: str
    line_start: int
    line_end: int
    column_start: int = 0
    column_end: int = 0


@dataclass
class Entity:
    """A code entity (function, class, module, etc.)."""

    id: str  # Unique identifier: file_path::qualified_name
    kind: EntityKind
    name: str  # Short name (e.g., "MyClass")
    qualified_name: str  # Full name (e.g., "my_pkg.module.MyClass")
    location: Location
    docstring: Optional[str] = None
    signature: Optional[str] = None
    source_code: Optional[str] = None
    # Flexible metadata storage. Common keys:
    # - "language": "python", "javascript", etc.
    # - "complexity": Cyclomatic complexity score
    # - "is_async": "true" if async function
    metadata: dict[str, str] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Entity):
            return False
        return self.id == other.id


@dataclass
class Relationship:
    """A relationship between two entities."""

    source_id: str
    target_id: str
    kind: RelationshipKind
    metadata: dict[str, str] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.source_id, self.target_id, self.kind))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Relationship):
            return False
        return (
            self.source_id == other.source_id
            and self.target_id == other.target_id
            and self.kind == other.kind
        )


@dataclass
class ParseResult:
    """Result from parsing a single file."""

    file_path: str
    entities: list[Entity]
    relationships: list[Relationship]
    errors: list[str] = field(default_factory=list)


@dataclass
class ChunkingConfig:
    """Configuration for code chunking."""

    max_chunk_size: int = 1000
    overlap: int = 100
    include_signatures: bool = True
    include_docstrings: bool = True


@dataclass
class CodeChunk:
    """A chunk of code for indexing and retrieval."""

    id: str  # Unique chunk ID: entity_id::chunk_index
    entity_id: str  # Parent entity ID
    content: str  # Raw text content
    tokens: list[str] = field(default_factory=list)  # BM25 tokens
    embedding: Optional[list[float]] = None  # Dense vector
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class EmbeddingConfig:
    """Configuration for embedding generation."""

    provider: str = "openai"  # "openai", "sentence-transformers", "local"
    model_name: str = "text-embedding-3-small"
    dimension: int = 1536
    batch_size: int = 100
    normalize: bool = True  # Normalize vectors for cosine similarity
