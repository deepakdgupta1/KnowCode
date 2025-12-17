"""Knowledge store with JSON persistence and querying."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from knowcode.graph_builder import GraphBuilder
from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)


class KnowledgeStore:
    """In-memory knowledge store with JSON persistence."""

    DEFAULT_FILENAME = "knowcode_knowledge.json"

    def __init__(self) -> None:
        """Initialize empty knowledge store."""
        self.entities: dict[str, Entity] = {}
        self.relationships: list[Relationship] = []
        self.metadata: dict[str, Any] = {}

    @classmethod
    def from_graph_builder(cls, builder: GraphBuilder) -> "KnowledgeStore":
        """Create store from a graph builder.

        Args:
            builder: GraphBuilder with parsed data.

        Returns:
            New KnowledgeStore instance.
        """
        store = cls()
        store.entities = builder.entities.copy()
        store.relationships = builder.relationships.copy()
        store.metadata = {
            "stats": builder.stats(),
            "errors": builder.errors,
        }
        return store

    def save(self, path: str | Path) -> None:
        """Save knowledge store to JSON file.

        Args:
            path: Path to save file (directory or file).
        """
        path = Path(path)
        if path.is_dir():
            path = path / self.DEFAULT_FILENAME

        data = {
            "version": "1.0",
            "metadata": self.metadata,
            "entities": {
                eid: self._entity_to_dict(e)
                for eid, e in self.entities.items()
            },
            "relationships": [
                self._relationship_to_dict(r)
                for r in self.relationships
            ],
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> "KnowledgeStore":
        """Load knowledge store from JSON file.

        Args:
            path: Path to load file from.

        Returns:
            Loaded KnowledgeStore instance.
        """
        path = Path(path)
        if path.is_dir():
            path = path / cls.DEFAULT_FILENAME

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        store = cls()
        store.metadata = data.get("metadata", {})

        for eid, edata in data.get("entities", {}).items():
            store.entities[eid] = cls._dict_to_entity(edata)

        for rdata in data.get("relationships", []):
            store.relationships.append(cls._dict_to_relationship(rdata))

        return store

    def _entity_to_dict(self, entity: Entity) -> dict[str, Any]:
        """Convert entity to dictionary."""
        return {
            "id": entity.id,
            "kind": entity.kind.value,
            "name": entity.name,
            "qualified_name": entity.qualified_name,
            "location": asdict(entity.location),
            "docstring": entity.docstring,
            "signature": entity.signature,
            "source_code": entity.source_code,
            "metadata": entity.metadata,
        }

    @staticmethod
    def _dict_to_entity(data: dict[str, Any]) -> Entity:
        """Convert dictionary to entity."""
        return Entity(
            id=data["id"],
            kind=EntityKind(data["kind"]),
            name=data["name"],
            qualified_name=data["qualified_name"],
            location=Location(**data["location"]),
            docstring=data.get("docstring"),
            signature=data.get("signature"),
            source_code=data.get("source_code"),
            metadata=data.get("metadata", {}),
        )

    def _relationship_to_dict(self, rel: Relationship) -> dict[str, Any]:
        """Convert relationship to dictionary."""
        return {
            "source_id": rel.source_id,
            "target_id": rel.target_id,
            "kind": rel.kind.value,
            "metadata": rel.metadata,
        }

    @staticmethod
    def _dict_to_relationship(data: dict[str, Any]) -> Relationship:
        """Convert dictionary to relationship."""
        return Relationship(
            source_id=data["source_id"],
            target_id=data["target_id"],
            kind=RelationshipKind(data["kind"]),
            metadata=data.get("metadata", {}),
        )

    # Query methods

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    def search(self, pattern: str) -> list[Entity]:
        """Search entities by name pattern."""
        pattern_lower = pattern.lower()
        return [
            e for e in self.entities.values()
            if pattern_lower in e.name.lower()
            or pattern_lower in e.qualified_name.lower()
        ]

    def get_callers(self, entity_id: str) -> list[Entity]:
        """Get entities that call the given entity."""
        caller_ids = [
            r.source_id for r in self.relationships
            if r.target_id == entity_id and r.kind == RelationshipKind.CALLS
        ]
        return [
            self.entities[cid] for cid in caller_ids
            if cid in self.entities
        ]

    def get_callees(self, entity_id: str) -> list[Entity]:
        """Get entities called by the given entity."""
        callee_ids = [
            r.target_id for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.CALLS
        ]
        return [
            self.entities[cid] for cid in callee_ids
            if cid in self.entities
        ]

    def get_imports(self, entity_id: str) -> list[str]:
        """Get imports for a module entity."""
        return [
            r.target_id for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.IMPORTS
        ]

    def get_children(self, entity_id: str) -> list[Entity]:
        """Get entities contained by the given entity."""
        child_ids = [
            r.target_id for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.CONTAINS
        ]
        return [
            self.entities[cid] for cid in child_ids
            if cid in self.entities
        ]

    def get_parent(self, entity_id: str) -> Optional[Entity]:
        """Get the parent entity (container) of an entity."""
        for r in self.relationships:
            if r.target_id == entity_id and r.kind == RelationshipKind.CONTAINS:
                return self.entities.get(r.source_id)
        return None

    def get_dependencies(self, entity_id: str) -> list[Entity]:
        """Get all dependencies of an entity (calls + imports)."""
        dep_ids = set()
        for r in self.relationships:
            if r.source_id == entity_id:
                if r.kind in {RelationshipKind.CALLS, RelationshipKind.IMPORTS}:
                    dep_ids.add(r.target_id)
        return [
            self.entities[did] for did in dep_ids
            if did in self.entities
        ]

    def get_dependents(self, entity_id: str) -> list[Entity]:
        """Get entities that depend on the given entity."""
        dependent_ids = set()
        for r in self.relationships:
            if r.target_id == entity_id:
                if r.kind in {RelationshipKind.CALLS, RelationshipKind.IMPORTS}:
                    dependent_ids.add(r.source_id)
        return [
            self.entities[did] for did in dependent_ids
            if did in self.entities
        ]

    def list_by_kind(self, kind: EntityKind) -> list[Entity]:
        """List all entities of a given kind."""
        return [e for e in self.entities.values() if e.kind == kind]
