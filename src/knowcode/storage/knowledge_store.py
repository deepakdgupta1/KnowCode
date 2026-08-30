"""Knowledge store with JSON persistence and querying."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.utils.atomic_write import atomic_write_json, cleanup_orphaned_temp_files
from knowcode.utils.entity_identity import (
    ensure_entity_content_hash,
    normalize_file_identity,
)


class KnowledgeStore:
    """In-memory knowledge store with JSON persistence."""

    DEFAULT_FILENAME = "knowcode_knowledge.json"
    SCHEMA_VERSION = 2
    LEGACY_SCHEMA_VERSION = 1
    LEGACY_VERSION = "1.0"
    SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}

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

        The format includes:
        - schema_version: Store schema version for compatibility.
        - metadata: Global scan stats and errors.
        - entities: Dictionary mapping Entity IDs to their full data.
        - relationships: List of all edges in the graph.

        Args:
            path: Path to save file (directory or file).
        """
        path = Path(path)
        if path.is_dir():
            path = path / self.DEFAULT_FILENAME

        data = {
            "schema_version": self.SCHEMA_VERSION,
            # Keep legacy field for backwards compatibility with older readers.
            "version": self.LEGACY_VERSION,
            "metadata": self.metadata,
            "entities": {
                eid: self._entity_to_dict(e) for eid, e in self.entities.items()
            },
            "relationships": [
                self._relationship_to_dict(r) for r in self.relationships
            ],
        }

        # Crash-safe replacement (Step 13): the previous graph stays loadable
        # until the new one has been written and synced in full.
        atomic_write_json(path, data, indent=2)

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

        # A staging file present at load time was abandoned by a crashed
        # process: this store is only ever published by one writer.
        cleanup_orphaned_temp_files(path.parent)

        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
        except json.JSONDecodeError as exc:
            # Pre-Step-13 saves truncated in place, so an interrupted one left
            # exactly this file behind. Fail closed with rebuild guidance.
            raise ValueError(
                f"Invalid or truncated knowledge store in {path}: {exc}. "
                "Re-run `knowcode build` to rebuild it."
            ) from exc
        if not isinstance(loaded_data, dict):
            raise ValueError(
                f"Invalid knowledge store format in {path}. Expected a JSON object."
            )
        raw_data: dict[str, Any] = loaded_data
        data = cls._migrate_schema(raw_data)

        store = cls()
        metadata = data.get("metadata", {})
        store.metadata = metadata if isinstance(metadata, dict) else {}

        entities_data = data.get("entities", {})
        if isinstance(entities_data, dict):
            for eid, edata in entities_data.items():
                if not isinstance(edata, dict):
                    continue
                store.entities[eid] = cls._dict_to_entity(edata)

        relationships_data = data.get("relationships", [])
        if isinstance(relationships_data, list):
            for rdata in relationships_data:
                if not isinstance(rdata, dict):
                    continue
                store.relationships.append(cls._dict_to_relationship(rdata))

        return store

    @classmethod
    def _migrate_schema(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate and migrate persisted store payloads across schema versions."""
        schema_version = data.get("schema_version")
        if schema_version is None:
            legacy_version = data.get("version")
            if legacy_version in (None, cls.LEGACY_VERSION, 1, "1"):
                migrated = dict(data)
                migrated["schema_version"] = cls.SCHEMA_VERSION
                return migrated
            raise ValueError(
                "Unsupported legacy knowledge store format. "
                f"Found version={legacy_version!r}. Re-run `knowcode build`."
            )

        normalized = cls._normalize_schema_version(schema_version)
        if normalized == cls.LEGACY_SCHEMA_VERSION:
            migrated = dict(data)
            migrated["schema_version"] = cls.SCHEMA_VERSION
            return migrated
        if normalized in cls.SUPPORTED_SCHEMA_VERSIONS:
            migrated = dict(data)
            migrated["schema_version"] = normalized
            return migrated

        raise ValueError(
            "Unsupported knowledge store schema version "
            f"{schema_version!r}. Supported versions: "
            f"{sorted(cls.SUPPORTED_SCHEMA_VERSIONS)}. "
            "Re-run `knowcode build` to rebuild persisted artifacts."
        )

    @staticmethod
    def _normalize_schema_version(value: Any) -> int:
        """Normalize schema versions represented as int/str."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        raise ValueError(f"Invalid schema version value: {value!r}")

    def _entity_to_dict(self, entity: Entity) -> dict[str, Any]:
        """Convert entity to dictionary."""
        ensure_entity_content_hash(entity)
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
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        entity = Entity(
            id=data["id"],
            kind=EntityKind(data["kind"]),
            name=data["name"],
            qualified_name=data["qualified_name"],
            location=Location(**data["location"]),
            docstring=data.get("docstring"),
            signature=data.get("signature"),
            source_code=data.get("source_code"),
            metadata=metadata,
        )
        ensure_entity_content_hash(entity)
        return entity

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
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return Relationship(
            source_id=data["source_id"],
            target_id=data["target_id"],
            kind=RelationshipKind(data["kind"]),
            metadata=metadata,
        )

    # Query methods

    def indexed_file_paths(self) -> set[str]:
        """Every source file this store holds entities for.

        Normalized to match the scanner's paths; see the SQLite store's copy
        for why freshness needs it (BL-23).
        """
        return {
            normalize_file_identity(entity.location.file_path)
            for entity in self.entities.values()
            if entity.location
            and entity.location.file_path
            and Path(entity.location.file_path).is_absolute()
        }

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    def search(self, pattern: str) -> list[Entity]:
        """Search entities by name pattern."""
        pattern_lower = pattern.lower()
        return [
            e
            for e in self.entities.values()
            if pattern_lower in e.name.lower()
            or pattern_lower in e.qualified_name.lower()
        ]

    def get_callers(self, entity_id: str) -> list[Entity]:
        """Get entities that call the given entity."""
        caller_ids = [
            r.source_id
            for r in self.relationships
            if r.target_id == entity_id and r.kind == RelationshipKind.CALLS
        ]
        return [self.entities[cid] for cid in caller_ids if cid in self.entities]

    def get_callees(self, entity_id: str) -> list[Entity]:
        """Get entities called by the given entity."""
        callee_ids = [
            r.target_id
            for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.CALLS
        ]
        return [self.entities[cid] for cid in callee_ids if cid in self.entities]

    def get_imports(self, entity_id: str) -> list[str]:
        """Get imports for a module entity."""
        return [
            r.target_id
            for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.IMPORTS
        ]

    def get_children(self, entity_id: str) -> list[Entity]:
        """Get entities contained by the given entity."""
        child_ids = [
            r.target_id
            for r in self.relationships
            if r.source_id == entity_id and r.kind == RelationshipKind.CONTAINS
        ]
        return [self.entities[cid] for cid in child_ids if cid in self.entities]

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
        return [self.entities[did] for did in dep_ids if did in self.entities]

    def get_dependents(self, entity_id: str) -> list[Entity]:
        """Get entities that depend on the given entity."""
        dependent_ids = set()
        for r in self.relationships:
            if r.target_id == entity_id:
                if r.kind in {RelationshipKind.CALLS, RelationshipKind.IMPORTS}:
                    dependent_ids.add(r.source_id)
        return [self.entities[did] for did in dependent_ids if did in self.entities]

    def list_by_kind(self, kind: EntityKind | str) -> list[Entity]:
        """List all entities of a given kind.

        Args:
            kind: EntityKind enum or its string value (e.g., "class", "commit").

        Returns:
            Entities matching the requested kind.
        """
        return self.get_entities_by_kind(kind)

    def get_entities_by_kind(self, kind: EntityKind | str) -> list[Entity]:
        """Return entities filtered by kind.

        Args:
            kind: EntityKind enum or its string value.

        Returns:
            Entities whose kind matches the provided value.
        """
        if isinstance(kind, str):
            try:
                kind = EntityKind(kind)
            except ValueError:
                return []
        return [e for e in self.entities.values() if e.kind == kind]

    def get_outgoing_relationships(self, entity_id: str) -> list[Relationship]:
        """Return relationships where the entity is the source."""
        return [r for r in self.relationships if r.source_id == entity_id]

    def get_incoming_relationships(self, entity_id: str) -> list[Relationship]:
        """Return relationships where the entity is the target."""
        return [r for r in self.relationships if r.target_id == entity_id]

    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Multi-hop call graph traversal.

        Traverses the call graph from a starting entity to find all callers
        or callees up to the specified depth.

        Args:
            entity_id: Starting entity ID.
            direction: "callers" (who calls this) or "callees" (what this calls).
            depth: Maximum traversal depth (1 = direct only).
            max_results: Maximum results to return.

        Returns:
            List of dicts with entity info and call_depth.
        """
        if direction not in ("callers", "callees"):
            raise ValueError(
                f"direction must be 'callers' or 'callees', got {direction}"
            )

        results: list[dict[str, Any]] = []
        visited: set[str] = {entity_id}
        frontier: list[tuple[str, int]] = [(entity_id, 0)]

        while frontier and len(results) < max_results:
            current_id, current_depth = frontier.pop(0)

            if current_depth >= depth:
                continue

            # Get next hop entities
            if direction == "callees":
                next_ids = [
                    r.target_id
                    for r in self.relationships
                    if r.source_id == current_id and r.kind == RelationshipKind.CALLS
                ]
            else:  # callers
                next_ids = [
                    r.source_id
                    for r in self.relationships
                    if r.target_id == current_id and r.kind == RelationshipKind.CALLS
                ]

            for next_id in next_ids:
                if next_id in visited:
                    continue

                visited.add(next_id)
                entity = self.entities.get(next_id)

                if entity:
                    results.append(
                        {
                            "entity_id": entity.id,
                            "name": entity.name,
                            "qualified_name": entity.qualified_name,
                            "kind": entity.kind.value,
                            "file": entity.location.file_path,
                            "line": entity.location.line_start,
                            "call_depth": current_depth + 1,
                        }
                    )

                    if current_depth + 1 < depth:
                        frontier.append((next_id, current_depth + 1))

        return results

    def get_impact(self, entity_id: str, max_depth: int = 3) -> dict[str, Any]:
        """Analyze the impact of modifying or deleting an entity.

        Returns direct dependents (callers, importers) and transitive dependents
        up to max_depth, with a risk score based on impact breadth.

        Args:
            entity_id: Entity to analyze impact for.
            max_depth: Maximum depth for transitive analysis.

        Returns:
            Dict with direct_dependents, transitive_dependents, and risk_score.
        """
        entity = self.entities.get(entity_id)
        if not entity:
            return {
                "entity_id": entity_id,
                "direct_dependents": [],
                "transitive_dependents": [],
                "risk_score": 0.0,
                "error": "Entity not found",
            }

        # Get direct dependents (1-hop)
        direct = self.trace_calls(
            entity_id, direction="callers", depth=1, max_results=100
        )

        # Get transitive dependents (multi-hop)
        transitive = self.trace_calls(
            entity_id, direction="callers", depth=max_depth, max_results=100
        )
        # Remove direct from transitive
        direct_ids = {d["entity_id"] for d in direct}
        transitive_only = [t for t in transitive if t["entity_id"] not in direct_ids]

        # Calculate risk score (0.0-1.0)
        # Based on: number of dependents, depth of impact, entity types affected
        direct_count = len(direct)
        transitive_count = len(transitive_only)

        # Higher risk if many dependents
        breadth_score = min(1.0, (direct_count + transitive_count * 0.5) / 20)

        # Higher risk if affects multiple files
        affected_files = {d.get("file") for d in direct + transitive_only}
        file_score = min(1.0, len(affected_files) / 5)

        # Higher risk if entity is a class or module (wider impact)
        type_score = 0.3
        if entity.kind == EntityKind.CLASS:
            type_score = 0.6
        elif entity.kind == EntityKind.MODULE:
            type_score = 0.8

        risk_score = round((breadth_score + file_score + type_score) / 3, 2)

        return {
            "entity_id": entity_id,
            "entity_name": entity.qualified_name,
            "direct_dependents": direct,
            "transitive_dependents": transitive_only,
            "total_affected": direct_count + transitive_count,
            "affected_files": list(affected_files),
            "risk_score": min(1.0, risk_score),
        }
