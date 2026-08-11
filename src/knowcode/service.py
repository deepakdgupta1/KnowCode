"""Service layer for KnowCode business logic."""

from __future__ import annotations

import os
import re
import shutil
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.config import AppConfig
from knowcode.errors import MissingKnowledgeStoreError, MissingSemanticIndexError
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.retrieval.orchestrator import RetrievalOrchestrator
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.llm.embedding import create_embedding_provider
from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_backends import create_vector_store
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.retrieval.exact_query_engine import ExactQueryEngine
from knowcode.data_models import TaskType

if TYPE_CHECKING:
    from knowcode.retrieval.orchestrator import SearchEngineProtocol


class KnowCodeService:
    """Service to handle core KnowCode operations."""

    def __init__(
        self,
        store_path: str | Path = ".",
        config_path: Optional[str] = None,
        app_config: Optional[AppConfig] = None,
        strict_config: bool = False,
    ) -> None:
        """Initialize service.

        Args:
            store_path: Path to load the knowledge store from.
            config_path: Optional config file path (aimodels.yaml).
            app_config: Optional pre-loaded AppConfig.
            strict_config: If True, invalid config files raise instead of
                silently falling back to defaults.
        """
        self.store_path = Path(store_path)
        self.app_config = app_config or AppConfig.load(
            config_path, strict=strict_config
        )
        self.app_config.apply_runtime_policy(source_root=self._store_root())
        self._store: Any = None
        self._search_engine: Optional["SearchEngine"] = None
        self._indexer: Optional["Indexer"] = None
        self._retrieval_orchestrator = RetrievalOrchestrator(self)

    @property
    def store(self) -> Any:
        """Get or load the knowledge store."""
        if self._store is None:
            store_file = self._assert_store_exists()
            if store_file.suffix == ".db":
                self._store = SqliteKnowledgeStore(store_file)
            else:
                self._store = KnowledgeStore.load(self.store_path)
        return self._store

    def _store_root(self) -> Path:
        """Resolve the root directory where store/index artifacts live."""
        return self.store_path if self.store_path.is_dir() else self.store_path.parent

    def _store_file(self) -> Path:
        """Resolve the knowledge store file path."""
        if self.store_path.is_dir():
            db_path = self.store_path / "knowledge.db"
            if db_path.exists():
                return db_path
            return self.store_path / KnowledgeStore.DEFAULT_FILENAME
        return self.store_path

    def _index_path(self) -> Path:
        """Resolve the semantic index directory path."""
        return self._store_root() / "knowcode_index"

    def _assert_store_exists(self) -> Path:
        """Validate that the persisted knowledge store exists."""
        store_file = self._store_file()
        if not store_file.exists():
            raise MissingKnowledgeStoreError(store_file)
        return store_file

    def _assert_index_exists(self) -> Path:
        """Validate that the persisted semantic index exists."""
        index_path = self._index_path()
        if not index_path.exists():
            raise MissingSemanticIndexError(index_path)
        return index_path

    def ensure_store(
        self,
        directory: Optional[str | Path] = None,
        output: Optional[str | Path] = None,
        ignore: list[str] | None = None,
        temporal: bool = False,
        coverage: str | Path | None = None,
    ) -> Path:
        """Ensure the knowledge store exists, building it only if missing."""
        store_file = self._store_file()
        if store_file.exists():
            return store_file

        source_dir = Path(directory) if directory is not None else self._store_root()
        output_path = Path(output) if output is not None else self._store_root()
        self.analyze(
            directory=source_dir,
            output=output_path,
            ignore=ignore,
            temporal=temporal,
            coverage=coverage,
        )
        db_path = output_path / "knowledge.db" if output_path.is_dir() else output_path.with_suffix(".db")
        if db_path.exists():
            return db_path
        return (
            output_path / KnowledgeStore.DEFAULT_FILENAME
            if output_path.is_dir()
            else output_path
        )

    def ensure_index(
        self,
        directory: Optional[str | Path] = None,
        index_path: Optional[str | Path] = None,
    ) -> Path:
        """Ensure the semantic index exists, building it only if missing."""
        resolved_index_path = (
            Path(index_path) if index_path is not None else self._index_path()
        )
        if resolved_index_path.exists():
            return resolved_index_path

        source_dir = Path(directory) if directory is not None else self._store_root()
        self._build_index(source_dir, resolved_index_path)
        return resolved_index_path

    def get_indexer(self, index_path: Optional[str | Path] = None) -> "Indexer":
        """Get or create the indexer.

        Args:
            index_path: Optional path to load an existing index from.

        Returns:
            Initialized Indexer instance.
        """
        if self._indexer is None:

            provider = create_embedding_provider(app_config=self.app_config)
            resolved_index_path = Path(index_path) if index_path else self._index_path()
            db_path = resolved_index_path / "chunks.db"
            chunk_repo = SqliteChunkRepository(db_path)

            dimension = provider.config.dimension
            vector_store = create_vector_store(
                self.app_config.vector_backend,
                dimension=dimension,
                index_dir=resolved_index_path,
            )

            self._indexer = Indexer(provider, chunk_repo=chunk_repo, vector_store=vector_store)

            if resolved_index_path.exists():
                self._indexer.load(resolved_index_path)

        return self._indexer

    def get_search_engine(
        self, index_path: Optional[str | Path] = None
    ) -> "SearchEngine":
        """Get or create the search engine.

        Args:
            index_path: Optional path to load an existing index from.

        Returns:
            SearchEngine wired to the current knowledge store.
        """
        if self._search_engine is None:

            indexer = self.get_indexer(index_path)
            hybrid_index = HybridIndex(
                indexer.chunk_repo, 
                indexer.vector_store,
                alpha=self.app_config.hybrid_alpha
            )

            self._search_engine = SearchEngine(
                indexer.chunk_repo,
                indexer.embedding_provider,
                hybrid_index,
                self.store,
                config=self.app_config,
            )
        return self._search_engine

    def get_exact_query_engine(self, index_path: Optional[str | Path] = None) -> "SearchEngineProtocol":
        """Build and return an ExactQueryEngine.
        
        Args:
            index_path: Directory containing the index. If None, uses default.
            
        Returns:
            ExactQueryEngine instance.
        """
        if index_path is None:
            index_path = self.ensure_index()
            
        return ExactQueryEngine(self.get_indexer(index_path).chunk_repo)

    def retrieve_context_for_query(
        self,
        query: str,
        max_tokens: int = 4000,
        task_type: Optional["TaskType"] = None,
        limit_entities: int = 3,
        per_entity_max_tokens: Optional[int] = None,
        expand_deps: bool = True,
        verbosity: str = "minimal",
        include_metadata: bool = False,
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
        freshness = self.get_freshness_metadata()
        is_stale = freshness.get("is_stale", False)

        res = self._retrieval_orchestrator.retrieve_context_for_query(
            query=query,
            max_tokens=max_tokens,
            task_type=task_type,
            limit_entities=limit_entities,
            per_entity_max_tokens=per_entity_max_tokens,
            expand_deps=expand_deps,
            verbosity=verbosity,
            include_metadata=include_metadata,
            is_stale=is_stale,
        )
        res["freshness"] = freshness

        # Log query to telemetry
        from knowcode.telemetry import log_event

        threshold = self.app_config.sufficiency_threshold

        score = res.get("sufficiency_score", 0.0)
        local_or_escalated = "local" if score >= threshold else "escalated"
        log_event(
            self.store_path,
            {
                "query": query,
                "verbosity": verbosity,
                "sufficiency_score": score,
                "is_stale": res["freshness"]["is_stale"],
                "local_or_escalated": local_or_escalated,
            },
        )

        return res

    def get_freshness_metadata(self) -> dict[str, Any]:
        """Compute freshness metadata for the knowledge store and index."""
        store_file = self._store_file()
        last_store_rebuild = 0.0
        if store_file.exists():
            last_store_rebuild = os.path.getmtime(store_file)

        index_manifest = self._index_path() / "index_manifest.json"
        last_index_rebuild = 0.0
        if index_manifest.exists():
            last_index_rebuild = os.path.getmtime(index_manifest)

        latest_source_change = 0.0
        is_stale = False
        stale_reasons = []

        try:
            from knowcode.indexing.scanner import Scanner

            root_dir = self._store_root()
            if root_dir.exists() and root_dir.is_dir():
                scanner = Scanner(root_dir)
                files = scanner.scan_all()
                if files:
                    latest_source_change = max(os.path.getmtime(f.path) for f in files)
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning("Failed to check source staleness (OS error): %s", e)

        if last_store_rebuild == 0.0:
            is_stale = True
            stale_reasons.append("knowledge_store_missing")
        elif latest_source_change > last_store_rebuild:
            is_stale = True
            stale_reasons.append("store_stale_source_changed")

        if last_index_rebuild == 0.0:
            is_stale = True
            stale_reasons.append("semantic_index_missing")
        elif latest_source_change > last_index_rebuild:
            is_stale = True
            stale_reasons.append("index_stale_source_changed")

        return {
            "last_store_rebuild": int(last_store_rebuild),
            "last_index_rebuild": int(last_index_rebuild),
            "latest_source_change": int(latest_source_change),
            "is_stale": is_stale,
            "stale_reasons": stale_reasons,
        }

    def _build_index(self, directory: str | Path, index_path: str | Path, incremental: bool = False) -> int:
        """Build a semantic index for a directory and persist it."""
        from knowcode.llm.embedding import create_embedding_provider
        from knowcode.indexing.indexer import Indexer
        from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

        provider = create_embedding_provider(app_config=self.app_config)
        resolved_index_path = Path(index_path)
        
        if not incremental:
            # Clear/initialize directory
            if resolved_index_path.exists():
                shutil.rmtree(resolved_index_path)
        
        resolved_index_path.mkdir(parents=True, exist_ok=True)
        
        db_path = resolved_index_path / "chunks.db"
        chunk_repo = SqliteChunkRepository(db_path)

        dimension = provider.config.dimension
        vector_store = create_vector_store(
            self.app_config.vector_backend,
            dimension=dimension,
            index_dir=resolved_index_path,
        )

        indexer = Indexer(provider, chunk_repo=chunk_repo, vector_store=vector_store)
        
        if incremental and (resolved_index_path / "index_manifest.json").exists():
            indexer.load(resolved_index_path)
            count = indexer.index_incremental(directory)
        else:
            count = indexer.index_directory(directory)
            
        indexer.save(resolved_index_path)
        return count

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
        keywords = [t for t in tokens if len(t) > 3 and t.lower() not in stopwords]
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
        ignore: list[str] | None = None,
        temporal: bool = False,
        coverage: str | Path | None = None,
        export_json: bool = False,
        incremental: bool = False,
    ) -> dict[str, Any]:
        """Analyze a codebase and persist the resulting knowledge store.

        Args:
            directory: Root directory to scan and parse.
            output: Destination path for the knowledge store JSON.
            ignore: Additional ignore patterns.
            temporal: Whether to include git history analysis.
            coverage: Optional Cobertura coverage report path.
            incremental: Whether to use incremental index build.

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

        output_path = Path(output)
        db_path = output_path / "knowledge.db" if output_path.is_dir() else output_path.with_suffix(".db")
        
        sqlite_store = SqliteKnowledgeStore(db_path)
        sqlite_store._conn.execute("BEGIN")
        try:
            for entity in builder.entities.values():
                sqlite_store.add_entity(entity)
            for rel in builder.relationships:
                sqlite_store.add_relationship(rel)
            sqlite_store._conn.execute("COMMIT")
        except Exception:
            sqlite_store._conn.execute("ROLLBACK")
            raise
            
        self._store = sqlite_store

        if export_json:
            store = KnowledgeStore.from_graph_builder(builder)
            json_path = output_path / KnowledgeStore.DEFAULT_FILENAME if output_path.is_dir() else output_path.with_suffix(".json")
            store.save(json_path)

        store_root = output_path if output_path.is_dir() else output_path.parent
        index_path = store_root / "knowcode_index"
        index_count = 0
        index_error: str | None = None
        try:
            index_count = self._build_index(Path(directory), index_path, incremental=incremental)
        except Exception as e:
            # Keep analyze usable without embedding credentials; semantic
            # indexing can still be built later with `knowcode index`.
            index_error = str(e)

        stats: dict[str, Any] = builder.stats()
        stats["indexed_chunks"] = index_count
        stats["index_path"] = str(index_path)
        if index_error:
            stats["index_error"] = index_error
        return stats

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
        summarize: bool = False,
        is_stale: bool = False,
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

        live_loader = None
        if is_stale:
            from knowcode.analysis.live_source_loader import LiveSourceLoader
            live_loader = LiveSourceLoader(self._store_root())

        synthesizer = ContextSynthesizer(self.store, max_tokens=max_tokens, live_loader=live_loader)

        # Use task-specific synthesis if task_type provided
        if task_type is not None:
            bundle = synthesizer.synthesize_with_task(
                entity.id, task_type, summarize=summarize
            )
        else:
            bundle = synthesizer.synthesize(entity.id, summarize=summarize)

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
        if hasattr(bundle, "task_type") and hasattr(bundle, "sufficiency_score"):
            result["task_type"] = (
                bundle.task_type.value if bundle.task_type else "general"
            )
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
        by_kind: dict[str, int] = {}
        rel_types: dict[str, int] = {}
        total_entities = 0
        total_relationships = 0

        if hasattr(self.store, "entities"):
            total_entities = len(self.store.entities)
            for entity in self.store.entities.values():
                kind = entity.kind.value
                by_kind[kind] = by_kind.get(kind, 0) + 1

            total_relationships = len(self.store.relationships)
            for rel in self.store.relationships:
                kind = rel.kind.value
                rel_types[kind] = rel_types.get(kind, 0) + 1
        elif isinstance(self.store, SqliteKnowledgeStore):
            cursor = self.store._conn.execute("SELECT kind, COUNT(*) as c FROM entities GROUP BY kind")
            for row in cursor:
                by_kind[row["kind"]] = row["c"]
            total_entities = sum(by_kind.values())

            cursor = self.store._conn.execute("SELECT kind, COUNT(*) as c FROM relationships GROUP BY kind")
            for row in cursor:
                rel_types[row["kind"]] = row["c"]
            total_relationships = sum(rel_types.values())

        stats = {
            "total_entities": total_entities,
            "entities_by_kind": by_kind,
            "total_relationships": total_relationships,
            "relationships_by_type": rel_types,
        }

        # Add index stats if indexer is loaded
        if self._indexer:
            stats["total_chunks"] = self._indexer.chunk_repo.count()
            if (
                hasattr(self._indexer.vector_store, "index")
                and self._indexer.vector_store.index
            ):
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
        return [
            {"id": c.id, "name": c.qualified_name, "file": c.location.file_path}
            for c in callers
        ]

    def reload(self) -> None:
        """Reload the knowledge store from disk.

        Useful when the underlying JSON file has been updated by a
        separate process (e.g., a CLI scan).
        """
        self._store = None
        try:
            # Force reload by accessing the property
            _ = self.store
        except FileNotFoundError as e:
            # If the file is gone, keep _store as None
            logging.getLogger(__name__).warning("Knowledge store file not found during reload: %s", e)

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
