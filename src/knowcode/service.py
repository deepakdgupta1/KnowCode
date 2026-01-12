"""Service layer for KnowCode business logic."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.config import AppConfig
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.storage.knowledge_store import KnowledgeStore

if TYPE_CHECKING:
    from knowcode.data_models import TaskType
    from knowcode.indexing.indexer import Indexer
    from knowcode.retrieval.search_engine import SearchEngine


class KnowCodeService:
    """Service to handle core KnowCode operations."""

    def __init__(
        self,
        store_path: str | Path = ".",
        config_path: Optional[str] = None,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        """Initialize service.

        Args:
            store_path: Path to load the knowledge store from.
            config_path: Optional config file path (aimodels.yaml).
            app_config: Optional pre-loaded AppConfig.
        """
        self.store_path = Path(store_path)
        self.app_config = app_config or AppConfig.load(config_path)
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
            from knowcode.llm.embedding import create_embedding_provider
            from knowcode.indexing.indexer import Indexer
            
            provider = create_embedding_provider(app_config=self.app_config)
            self._indexer = Indexer(provider)
            
            if index_path:
                self._indexer.load(Path(index_path))
            else:
                store_root = self.store_path if self.store_path.is_dir() else self.store_path.parent
                default_index = store_root / "knowcode_index"
                if default_index.exists():
                    self._indexer.load(default_index)
                
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
                self.store,
                config=self.app_config,
            )
        return self._search_engine

    def retrieve_context_for_query(
        self,
        query: str,
        max_tokens: int = 6000,
        task_type: Optional["TaskType"] = None,
        limit_entities: int = 3,
        per_entity_max_tokens: Optional[int] = None,
        expand_deps: bool = True,
    ) -> dict[str, Any]:
        """Retrieve an evidence-backed context bundle for a natural-language query.

        This is the unified retrieval entrypoint that both CLI Q&A and MCP tools
        should use to ensure consistent retrieval quality.

        Args:
            query: Natural-language query.
            max_tokens: Overall token budget across all returned entity bundles.
            task_type: Optional task type override; if omitted, query is classified.
            limit_entities: Maximum number of unique entities to include.
            per_entity_max_tokens: Optional per-entity token budget; defaults to an even split.
            expand_deps: Whether to expand dependency context during retrieval.

        Returns:
            Dictionary with context_text, sufficiency_score, evidence, and metadata.
        """
        from knowcode.llm.query_classifier import classify_query

        errors: list[str] = []

        detected_task_type, confidence = classify_query(query)
        resolved_task_type = task_type or detected_task_type
        task_confidence = 1.0 if task_type is not None else confidence

        if limit_entities <= 0 or max_tokens <= 0:
            return {
                "query": query,
                "task_type": resolved_task_type.value,
                "task_confidence": task_confidence,
                "retrieval_mode": "none",
                "context_text": "",
                "total_tokens": 0,
                "max_tokens": max_tokens,
                "truncated": False,
                "sufficiency_score": 0.0,
                "selected_entities": [],
                "evidence": [],
                "errors": ["Invalid token or entity limits."],
            }

        if per_entity_max_tokens is None:
            per_entity_max_tokens = max(200, min(2000, max_tokens // limit_entities))

        store_root = self.store_path if self.store_path.is_dir() else self.store_path.parent
        index_path = store_root / "knowcode_index"

        selected_entity_ids: list[str] = []
        evidence: list[dict[str, Any]] = []
        retrieval_mode = "lexical"

        if index_path.exists():
            try:
                engine = self.get_search_engine()
                self._validate_index_compatibility(index_path)
                scored = engine.search_scored(
                    query,
                    limit=max(10, limit_entities * 5),
                    expand_deps=expand_deps,
                )
                retrieval_mode = "semantic"

                primary = [s for s in scored if s.source == "retrieved"]
                seen_entities: set[str] = set()
                for s in primary:
                    if s.chunk.entity_id in seen_entities:
                        continue
                    seen_entities.add(s.chunk.entity_id)
                    selected_entity_ids.append(s.chunk.entity_id)
                    if len(selected_entity_ids) >= limit_entities:
                        break

                for rank, s in enumerate(scored, start=1):
                    evidence.append(
                        {
                            "rank": rank,
                            "chunk_id": s.chunk.id,
                            "entity_id": s.chunk.entity_id,
                            "score": s.score,
                            "source": s.source,
                        }
                    )

            except Exception as e:
                errors.append(f"Semantic retrieval failed; falling back to lexical: {e}")
                retrieval_mode = "lexical"

        if retrieval_mode == "lexical":
            candidates: list[str] = []
            seen: set[str] = set()

            def add_entity_ids(items: list[dict[str, Any]]) -> None:
                for item in items:
                    entity_id = item.get("id")
                    if not entity_id or entity_id in seen:
                        continue
                    seen.add(entity_id)
                    candidates.append(entity_id)

            add_entity_ids(self.search(query))
            if len(candidates) < limit_entities:
                for kw in self._extract_query_keywords(query):
                    add_entity_ids(self.search(kw))
                    if len(candidates) >= limit_entities:
                        break

            selected_entity_ids = candidates[:limit_entities]
            for rank, entity_id in enumerate(selected_entity_ids, start=1):
                evidence.append({"rank": rank, "entity_id": entity_id, "source": "lexical"})

        selected_entities: list[dict[str, Any]] = []
        context_parts: list[str] = []
        sufficiency_scores: list[float] = []
        total_tokens = 0
        truncated = False

        for entity_id in selected_entity_ids:
            try:
                bundle = self.get_context(
                    entity_id,
                    max_tokens=per_entity_max_tokens,
                    task_type=resolved_task_type,
                )
            except Exception as e:
                errors.append(f"Failed to synthesize context for {entity_id}: {e}")
                continue

            context_parts.append(bundle.get("context_text", ""))
            total_tokens += int(bundle.get("total_tokens", 0))
            truncated = truncated or bool(bundle.get("truncated", False))

            s = bundle.get("sufficiency_score")
            if isinstance(s, (int, float)):
                sufficiency_scores.append(float(s))

            selected_entities.append(
                {
                    "entity_id": bundle.get("entity_id", entity_id),
                    "task_type": bundle.get("task_type", resolved_task_type.value),
                    "total_tokens": bundle.get("total_tokens", 0),
                    "truncated": bundle.get("truncated", False),
                    "sufficiency_score": bundle.get("sufficiency_score", 0.0),
                }
            )

        context_text = "\n\n---\n\n".join([p for p in context_parts if p])
        sufficiency = (
            round(sum(sufficiency_scores) / len(sufficiency_scores), 2)
            if sufficiency_scores
            else 0.0
        )

        return {
            "query": query,
            "task_type": resolved_task_type.value,
            "task_confidence": task_confidence,
            "retrieval_mode": retrieval_mode,
            "context_text": context_text,
            "total_tokens": total_tokens,
            "max_tokens": max_tokens,
            "truncated": truncated,
            "sufficiency_score": sufficiency,
            "selected_entities": selected_entities,
            "evidence": evidence,
            "errors": errors,
        }

    def _extract_query_keywords(self, query: str) -> list[str]:
        """Extract identifier-like keywords from a natural-language query."""
        stopwords = {
            "how",
            "what",
            "where",
            "when",
            "why",
            "who",
            "does",
            "did",
            "is",
            "are",
            "can",
            "will",
            "the",
            "a",
            "an",
            "in",
            "on",
            "at",
            "for",
            "to",
            "of",
            "and",
            "or",
        }
        tokens = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_.]+\b", query)
        keywords = [
            t
            for t in tokens
            if len(t) > 3 and t.lower() not in stopwords
        ]
        return keywords[:10]

    def _validate_index_compatibility(self, index_path: Path) -> None:
        """Validate the loaded index against the current embedding configuration.

        Raises:
            ValueError: If the index manifest indicates an incompatible embedding model.
        """
        indexer = self.get_indexer(index_path=index_path)

        # Always enforce dimension compatibility to prevent runtime FAISS errors.
        expected_dim = int(getattr(indexer.embedding_provider.config, "dimension", 0))
        actual_dim = int(getattr(indexer.vector_store, "dimension", 0))
        if expected_dim and actual_dim and expected_dim != actual_dim:
            raise ValueError(
                "Embedding dimension mismatch between configured embedding model "
                f"({expected_dim}) and loaded index ({actual_dim}). Rebuild the "
                "semantic index with `knowcode index` using the same embedding model."
            )

        manifest = getattr(indexer, "manifest", {}) or {}
        embedding_meta = manifest.get("embedding")
        if not isinstance(embedding_meta, dict):
            return

        mismatches: list[str] = []
        current = indexer.embedding_provider.config

        for key in ("provider", "model_name", "dimension", "normalize"):
            if key not in embedding_meta:
                continue
            recorded = embedding_meta.get(key)
            current_val = getattr(current, key, None)
            if recorded != current_val:
                mismatches.append(f"{key}: index={recorded!r} current={current_val!r}")

        if mismatches:
            details = "; ".join(mismatches)
            raise ValueError(
                "Index embedding configuration mismatch. "
                f"{details}. Rebuild the semantic index with `knowcode index` "
                "using the same embedding model and settings."
            )

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
