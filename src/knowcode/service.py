"""Service layer for KnowCode business logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.data_models import EmbeddingConfig


class KnowCodeService:
    """Service to handle core KnowCode operations."""

    def __init__(self, store_path: str | Path = ".") -> None:
        """Initialize service.

        Args:
            store_path: Path to load the knowledge store from.
        """
        self.store_path = Path(store_path)
        self._store: Optional[KnowledgeStore] = None
        self._search_engine: Optional["SearchEngine"] = None
        self._indexer: Optional["Indexer"] = None

    @property
    def store(self) -> KnowledgeStore:
        """Get or load the knowledge store."""
        if self._store is None:
            self._store = KnowledgeStore.load(self.store_path)
        return self._store

    def get_indexer(self, index_path: Optional[str | Path] = None) -> "Indexer":
        """Get or create the indexer.

        Args:
            index_path: Optional path to load an existing index from.

        Returns:
            Initialized Indexer instance.
        """
        if self._indexer is None:
            from knowcode.llm.embedding import OpenAIEmbeddingProvider
            from knowcode.indexing.indexer import Indexer
            
            config = EmbeddingConfig()
            provider = OpenAIEmbeddingProvider(config)
            self._indexer = Indexer(provider)
            
            if index_path:
                self._indexer.load(Path(index_path))
            elif (self.store_path.parent / "knowcode_index").exists():
                self._indexer.load(self.store_path.parent / "knowcode_index")
                
        return self._indexer

    def get_search_engine(self, index_path: Optional[str | Path] = None) -> "SearchEngine":
        """Get or create the search engine.

        Args:
            index_path: Optional path to load an existing index from.

        Returns:
            SearchEngine wired to the current knowledge store.
        """
        if self._search_engine is None:
            from knowcode.retrieval.hybrid_index import HybridIndex
            from knowcode.retrieval.search_engine import SearchEngine
            
            indexer = self.get_indexer(index_path)
            hybrid_index = HybridIndex(indexer.chunk_repo, indexer.vector_store)
            
            self._search_engine = SearchEngine(
                indexer.chunk_repo, 
                indexer.embedding_provider, 
                hybrid_index, 
                self.store
            )
        return self._search_engine

    def analyze(
        self,
        directory: str | Path,
        output: str | Path,
        ignore: list[str] = None,
        temporal: bool = False,
        coverage: str | Path = None,
    ) -> dict[str, Any]:
        """Analyze a codebase and persist the resulting knowledge store.

        Args:
            directory: Root directory to scan and parse.
            output: Destination path for the knowledge store JSON.
            ignore: Additional ignore patterns.
            temporal: Whether to include git history analysis.
            coverage: Optional Cobertura coverage report path.

        Returns:
            Statistics from the graph builder.
        """
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
        """Search entities by pattern.

        Args:
            pattern: Substring match over names and qualified names.

        Returns:
            Lightweight entity metadata for display or API responses.
        """
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

    def get_context(
        self,
        target: str,
        max_tokens: int = 2000,
        task_type: Optional["TaskType"] = None,
    ) -> dict[str, Any]:
        """Get a context bundle for an entity.

        Args:
            target: Entity ID or search pattern.
            max_tokens: Maximum token budget for the context bundle.
            task_type: Optional task type for context prioritization.

        Returns:
            Dictionary containing context text and metadata.

        Raises:
            ValueError: If no matching entity is found or context synthesis fails.
        """
        from knowcode.data_models import TaskType as DataTaskType
        
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
        
        # Use task-specific synthesis if task_type provided
        if task_type is not None:
            bundle = synthesizer.synthesize_with_task(entity.id, task_type)
        else:
            bundle = synthesizer.synthesize(entity.id)
        
        if not bundle:
             raise ValueError(f"Failed to synthesize context for {entity.id}")

        result = {
            "entity_id": bundle.target_entity.id,
            "context_text": bundle.context_text,
            "total_tokens": bundle.total_tokens,
            "truncated": bundle.truncated,
            "included_entities": bundle.included_entities,
        }
        
        # Add task-specific fields if using task synthesis
        if hasattr(bundle, 'task_type') and hasattr(bundle, 'sufficiency_score'):
            result["task_type"] = bundle.task_type.value if bundle.task_type else "general"
            result["sufficiency_score"] = bundle.sufficiency_score
        else:
            result["task_type"] = "general"
            result["sufficiency_score"] = 0.0
            
        return result

    def get_stats(self) -> dict[str, Any]:
        """Get statistics from the current store.

        Returns:
            Aggregated counts of entities, relationships, and index state.
        """
        # This is slightly different from builder.stats() as we might not have the builder
        by_kind: dict[str, int] = {}
        for entity in self.store.entities.values():
            kind = entity.kind.value
            by_kind[kind] = by_kind.get(kind, 0) + 1

        rel_types: dict[str, int] = {}
        for rel in self.store.relationships:
            kind = rel.kind.value
            rel_types[kind] = rel_types.get(kind, 0) + 1

        stats = {
            "total_entities": len(self.store.entities),
            "entities_by_kind": by_kind,
            "total_relationships": len(self.store.relationships),
            "relationships_by_type": rel_types,
        }
        
        # Add index stats if indexer is loaded
        if self._indexer:
            stats["total_chunks"] = len(self._indexer.chunk_repo._chunks)
            if hasattr(self._indexer.vector_store, "index") and self._indexer.vector_store.index:
                stats["vector_index_size"] = self._indexer.vector_store.index.ntotal
                
        return stats

    def get_callers(self, entity_id: str) -> list[dict[str, Any]]:
        """Get callers of an entity.

        Args:
            entity_id: Entity ID to look up.

        Returns:
            Caller metadata dictionaries.
        """
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
        
        # Convert to dictionary (using internal helper or creating one)
        # We can reuse the knowledge store's _entity_to_dict if exposed, 
        # or just construct it manually here to be safe and explicit.
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
        """Get callees of an entity.

        Args:
            entity_id: Entity ID to look up.

        Returns:
            Callee metadata dictionaries.
        """
        callees = self.store.get_callees(entity_id)
        return [{"id": c.id, "name": c.qualified_name} for c in callees]
