"""Data models for KnowCode entities and relationships."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EntityKind(str, Enum):
    """Types of code entities."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    # Documentation entities
    DOCUMENT = "document"
    SECTION = "section"
    # Configuration entities
    CONFIG_KEY = "config_key"
    # Temporal entities
    COMMIT = "commit"
    AUTHOR = "author"
    # Runtime entities
    TEST_RUN = "test_run"
    COVERAGE_REPORT = "coverage_report"


class RelationshipKind(str, Enum):
    """Types of relationships between entities."""

    CALLS = "calls"
    IMPORTS = "imports"
    CONTAINS = "contains"
    INHERITS = "inherits"
    REFERENCES = "references"
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

    id: str  # file_path::qualified_name
    kind: EntityKind
    name: str
    qualified_name: str
    location: Location
    docstring: Optional[str] = None
    signature: Optional[str] = None
    source_code: Optional[str] = None
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
