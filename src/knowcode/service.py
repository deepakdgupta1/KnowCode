"""Service layer for KnowCode business logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from knowcode.context_synthesizer import ContextSynthesizer
from knowcode.graph_builder import GraphBuilder
from knowcode.knowledge_store import KnowledgeStore


class KnowCodeService:
    """Service to handle core KnowCode operations."""

    def __init__(self, store_path: str | Path = ".") -> None:
        """Initialize service.

        Args:
            store_path: Path to load the knowledge store from.
        """
        self.store_path = Path(store_path)
        self._store: Optional[KnowledgeStore] = None

    @property
    def store(self) -> KnowledgeStore:
        """Get or load the knowledge store."""
        if self._store is None:
            self._store = KnowledgeStore.load(self.store_path)
        return self._store

    def analyze(
        self,
        directory: str | Path,
        output: str | Path,
        ignore: list[str] = None,
        temporal: bool = False,
        coverage: str | Path = None,
    ) -> dict[str, Any]:
        """Analyze a codebase and save to store."""
        builder = GraphBuilder()
        builder.build_from_directory(
            root_dir=directory,
            additional_ignores=ignore,
            analyze_temporal=temporal,
            coverage_path=Path(coverage) if coverage else None,
        )

        store = KnowledgeStore.from_graph_builder(builder)
        output_path = Path(output)
        store.save(output_path)
        self._store = store
        
        return builder.stats()

    def search(self, pattern: str) -> list[dict[str, Any]]:
        """Search entities by pattern."""
        entities = self.store.search(pattern)
        return [
            {
                "id": e.id,
                "kind": e.kind.value,
                "name": e.name,
                "qualified_name": e.qualified_name,
                "file": e.location.file_path,
                "line": e.location.line_start,
            }
            for e in entities
        ]

    def get_context(self, target: str, max_tokens: int = 2000) -> dict[str, Any]:
        """Get context bundle for an entity."""
        # Try exact match first
        entity = self.store.get_entity(target)
        if not entity:
            # Try search
            matches = self.store.search(target)
            if matches:
                entity = matches[0]

        if not entity:
            raise ValueError(f"Entity not found: {target}")

        synthesizer = ContextSynthesizer(self.store, max_tokens=max_tokens)
        bundle = synthesizer.synthesize(entity.id)
        
        if not bundle:
             raise ValueError(f"Failed to synthesize context for {entity.id}")

        return {
            "entity_id": bundle.target_entity.id,
            "context_text": bundle.context_text,
            "total_tokens": bundle.total_tokens,
            "truncated": bundle.truncated,
            "included_entities": bundle.included_entities,
        }

    def get_stats(self) -> dict[str, Any]:
        """Get statistics from the current store."""
        # This is slightly different from builder.stats() as we might not have the builder
        by_kind: dict[str, int] = {}
        for entity in self.store.entities.values():
            kind = entity.kind.value
            by_kind[kind] = by_kind.get(kind, 0) + 1

        rel_types: dict[str, int] = {}
        for rel in self.store.relationships:
            kind = rel.kind.value
            rel_types[kind] = rel_types.get(kind, 0) + 1

        return {
            "total_entities": len(self.store.entities),
            "entities_by_kind": by_kind,
            "total_relationships": len(self.store.relationships),
            "relationships_by_type": rel_types,
        }

    def get_callers(self, entity_id: str) -> list[dict[str, Any]]:
        """Get callers of an entity."""
        callers = self.store.get_callers(entity_id)
        return [{"id": c.id, "name": c.qualified_name, "file": c.location.file_path} for c in callers]

    def reload(self) -> None:
        """Reload the knowledge store from disk.
        
        Useful when the underlying JSON file has been updated by a 
        separate process (e.g., a CLI scan).
        """
        self._store = None
        try:
            # Force reload by accessing the property
            _ = self.store
        except FileNotFoundError:
            # If the file is gone, keep _store as None
            pass

    def get_entity_details(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Get detailed information about an entity as a dictionary.
        
        This returns the raw structured data including source code, 
        docstrings, and metadata, which is useful for tool-calling agents.
        """
        entity = self.store.get_entity(entity_id)
        if not entity:
            return None
        
        from dataclasses import asdict
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

    def get_callees(self, entity_id: str) -> list[dict[str, Any]]:
        """Get callees of an entity."""
        callees = self.store.get_callees(entity_id)
        return [{"id": c.id, "name": c.qualified_name} for c in callees]
