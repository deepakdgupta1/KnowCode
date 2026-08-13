"""Service layer for KnowCode business logic."""

from __future__ import annotations

import os
import re
import shutil
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.config import AppConfig
from knowcode.errors import MissingKnowledgeStoreError, MissingSemanticIndexError
from knowcode.indexing import generations
from knowcode.indexing.generations import ResolvedGeneration
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

logger = logging.getLogger(__name__)

#: Build stages a generation can fail in, reported as ``index_error_stage``.
STAGE_KNOWLEDGE_STORE = "knowledge_store"
STAGE_SEMANTIC_INDEX = "semantic_index"
STAGE_PUBLICATION = "publication"


@dataclass(frozen=True)
class GenerationBuildResult:
    """Outcome of one complete-generation build (Step 14).

    ``published`` is the only success signal. A build that failed anywhere
    after the graph was parsed reports ``published=False`` with a classified
    stage, and the previously published generation is still the current one.
    """

    published: bool
    generation_id: Optional[str] = None
    kind: Optional[str] = None
    chunk_count: int = 0
    error: Optional[str] = None
    stage: Optional[str] = None
    graph_stats: dict[str, Any] = field(default_factory=dict)


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
        self._generation: Optional[ResolvedGeneration] = None
        self._generation_resolved = False
        self._retrieval_orchestrator = RetrievalOrchestrator(self)

    @property
    def store(self) -> Any:
        """Get or load the knowledge store."""
        if self._store is None:
            store_file = self._assert_store_exists()
            if store_file.suffix == ".db":
                self._store = SqliteKnowledgeStore(store_file)
            else:
                self._store = KnowledgeStore.load(store_file)
        return self._store

    def _store_root(self) -> Path:
        """Resolve the root directory where store/index artifacts live."""
        return self.store_path if self.store_path.is_dir() else self.store_path.parent

    # ------------------------------------------------------------------
    # Index generations (Step 14, ADR 4)
    # ------------------------------------------------------------------

    def current_generation(self) -> Optional[ResolvedGeneration]:
        """Resolve the published generation this service reads from.

        Resolved once and cached so the knowledge store, chunk repository,
        vector store, and search engine handed out by this instance all come
        from one generation. ``reload()`` is the only way to move onto a newer
        one; handing *concurrent readers* between generations is Step 18.
        """
        if not self._generation_resolved:
            self._generation = generations.resolve_current_generation(
                self._index_path()
            )
            self._generation_resolved = True
        return self._generation

    def _artifact_dir(self, index_path: Optional[str | Path] = None) -> Path:
        """Directory holding the chunk/vector artifacts a reader should open.

        A published generation with a semantic index owns its own directory.
        Otherwise this is the flat index root, which is both the pre-Step-14
        layout and where a not-yet-built index would go. A graph-only
        generation deliberately does not route here: opening a chunk store
        inside it would create ``chunks.db`` in an immutable generation.
        """
        if index_path is not None:
            root = Path(index_path)
            generation = generations.resolve_current_generation(root)
            if generation is not None and generation.has_semantic_index:
                return generation.path
            return root

        generation = self.current_generation()
        if generation is not None and generation.has_semantic_index:
            return generation.path
        return self._index_path()

    def _store_file(self) -> Path:
        """Resolve the knowledge store file path.

        A published generation owns the authoritative ``knowledge.db``. Without
        one, the legacy flat layout is used so pre-Step-14 installs stay
        readable.
        """
        generation = self.current_generation()
        if generation is not None:
            return generation.knowledge_db

        if self.store_path.is_dir():
            db_path = self.store_path / "knowledge.db"
            if db_path.exists():
                return db_path
            return self.store_path / KnowledgeStore.DEFAULT_FILENAME
        return self.store_path

    def _index_path(self) -> Path:
        """Resolve the semantic index root (the generation artifact root)."""
        return self._store_root() / "knowcode_index"

    def _assert_store_exists(self) -> Path:
        """Validate that the persisted knowledge store exists."""
        store_file = self._store_file()
        if not store_file.exists():
            raise MissingKnowledgeStoreError(store_file)
        return store_file

    def _assert_index_exists(self) -> Path:
        """Validate that a searchable semantic index exists.

        A graph-only generation has a knowledge store but no chunks or vectors,
        so it fails here with the same actionable error as a missing index
        rather than presenting an empty index as a working one.
        """
        generation = self.current_generation()
        if generation is not None:
            if not generation.has_semantic_index:
                raise MissingSemanticIndexError(self._index_path())
            return generation.path

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
        resolved = self._store_file()
        if resolved.exists():
            return resolved
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
        """Ensure a complete published generation exists, building if not.

        "Exists" means a validated generation carrying a semantic index — not
        merely a directory. A pre-Step-14 flat index, a half-published
        generation, or a graph-only generation all rebuild, because none of
        them can be proven to match the graph they are searched alongside.
        """
        resolved_index_path = (
            Path(index_path) if index_path is not None else self._index_path()
        )
        generation = generations.resolve_current_generation(resolved_index_path)
        if generation is not None and generation.has_semantic_index:
            return resolved_index_path

        source_dir = Path(directory) if directory is not None else self._store_root()
        self._build_index(source_dir, resolved_index_path)
        return resolved_index_path

    def get_indexer(self, index_path: Optional[str | Path] = None) -> "Indexer":
        """Get or create the indexer.

        Args:
            index_path: Optional index root to resolve a generation from.

        Returns:
            Initialized Indexer instance.
        """
        if self._indexer is None:

            provider = create_embedding_provider(app_config=self.app_config)
            artifact_dir = self._artifact_dir(index_path)
            db_path = artifact_dir / "chunks.db"
            chunk_repo = SqliteChunkRepository(db_path)

            dimension = provider.config.dimension
            vector_store = create_vector_store(
                self.app_config.vector_backend,
                dimension=dimension,
                index_dir=artifact_dir,
            )

            self._indexer = Indexer(provider, chunk_repo=chunk_repo, vector_store=vector_store)

            if artifact_dir.exists():
                self._indexer.load(artifact_dir)

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

        # The generation pointer is the publication timestamp: it is written
        # last, so its mtime is the moment the index became searchable. Fall
        # back to the flat manifest for a pre-Step-14 layout.
        last_index_rebuild = 0.0
        pointer = generations.pointer_path(self._index_path())
        index_manifest = self._index_path() / "index_manifest.json"
        if pointer.exists():
            last_index_rebuild = os.path.getmtime(pointer)
        elif index_manifest.exists():
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

    def _build_index(
        self, directory: str | Path, index_path: str | Path, incremental: bool = False
    ) -> int:
        """Publish a complete generation for ``directory`` and return its chunk count.

        Raises:
            RuntimeError: The generation could not be published. The previously
                published generation, if any, is untouched.
        """
        result = self.build_generation(
            directory, index_path, incremental=incremental
        )
        if not result.published or result.kind != generations.KIND_FULL:
            raise RuntimeError(
                f"Semantic index build failed at stage {result.stage!r}: "
                f"{result.error}"
            )
        return result.chunk_count

    def build_generation(
        self,
        directory: str | Path,
        index_path: str | Path,
        *,
        ignore: list[str] | None = None,
        temporal: bool = False,
        coverage: Path | None = None,
        incremental: bool = False,
        builder: Optional[GraphBuilder] = None,
    ) -> GenerationBuildResult:
        """Stage, validate, and atomically publish one complete generation.

        Every artifact — ``knowledge.db``, ``chunks.db``, the vector index, and
        the manifest describing them — is built inside a staging directory that
        no reader can see. Only after they validate *together* is the staging
        directory renamed into ``generations/`` and the pointer replaced. A
        failure at any point leaves the previously published generation
        current, which is the defect this step exists to fix.

        A graph parse failure propagates: nothing has been staged and there is
        no artifact story to tell. Every failure after that is classified into
        :class:`GenerationBuildResult` so a caller can report it without
        mistaking it for success.
        """
        from knowcode.llm.embedding import create_embedding_provider
        from knowcode.indexing.indexer import Indexer
        from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

        index_root = Path(index_path)
        index_root.mkdir(parents=True, exist_ok=True)

        # Startup hygiene: a staging generation from a crashed writer is never
        # a publication candidate, so remove it before adding another.
        generations.cleanup_staging_generations(index_root)

        previous = generations.resolve_current_generation(index_root)

        if builder is None:
            builder = GraphBuilder()
            builder.build_from_directory(
                root_dir=directory,
                additional_ignores=ignore,
                analyze_temporal=temporal,
                coverage_path=coverage,
            )
        graph_stats: dict[str, Any] = builder.stats()

        provider = create_embedding_provider(app_config=self.app_config)
        dimension = provider.config.dimension

        with generations.staged_generation(index_root) as staging:
            entity_ids = list(builder.entities)
            try:
                self._write_staged_knowledge_store(staging.path, builder)
            except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                logger.exception("Failed to write the staged knowledge store")
                return GenerationBuildResult(
                    published=False,
                    error=str(exc),
                    stage=STAGE_KNOWLEDGE_STORE,
                    graph_stats=graph_stats,
                )

            chunk_count = 0
            chunk_ids: list[str] = []
            vector_count = 0
            semantic_error: Optional[Exception] = None

            chunk_repo: Optional[SqliteChunkRepository] = None
            try:
                if incremental and previous is not None and previous.has_semantic_index:
                    self._seed_staging_from(previous, staging.path)

                chunk_repo = SqliteChunkRepository(
                    staging.path / "chunks.db", dimension=dimension
                )
                vector_store = create_vector_store(
                    self.app_config.vector_backend,
                    dimension=dimension,
                    index_dir=staging.path,
                )
                indexer = Indexer(
                    provider, chunk_repo=chunk_repo, vector_store=vector_store
                )
                if incremental and (staging.path / "index_manifest.json").exists():
                    indexer.load(staging.path)
                    chunk_count = indexer.index_incremental(directory)
                else:
                    chunk_count = indexer.index_directory(directory, builder=builder)

                indexer.save(staging.path)
                vector_count = vector_store.count()
            except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                semantic_error = exc
                logger.warning("Semantic index build failed: %s", exc)
            finally:
                if chunk_repo is not None:
                    # Close before digesting: an open WAL connection means the
                    # committed rows are not all in ``chunks.db`` yet.
                    chunk_repo.close()

            if semantic_error is not None:
                if previous is not None and previous.has_semantic_index:
                    # Never trade a searchable generation for one without an
                    # index; the caller reports the failure instead.
                    return GenerationBuildResult(
                        published=False,
                        error=str(semantic_error),
                        stage=STAGE_SEMANTIC_INDEX,
                        graph_stats=graph_stats,
                    )
                kind = generations.KIND_GRAPH_ONLY
                self._discard_semantic_artifacts(staging.path)
                chunk_ids = []
                chunk_count = 0
                vector_count = 0
            else:
                kind = generations.KIND_FULL

            try:
                if kind == generations.KIND_FULL:
                    chunk_ids = generations.read_chunk_ids(staging.path / "chunks.db")
                    self._assert_chunk_vector_parity(chunk_ids, vector_count)
                manifest = generations.build_manifest(
                    staging.path,
                    generation_id=staging.generation_id,
                    kind=kind,
                    entity_ids=entity_ids,
                    relationship_count=len(builder.relationships),
                    chunk_ids=chunk_ids,
                    vector_count=vector_count,
                    embedding=self._embedding_metadata(provider),
                    vector={
                        "backend": self.app_config.vector_backend,
                        "dimension": dimension,
                    },
                    schema_versions={
                        "chunks": SqliteChunkRepository.SCHEMA_VERSION,
                        "index_manifest": Indexer.SCHEMA_VERSION,
                    },
                )
                published = generations.publish_generation(
                    index_root, staging.path, manifest
                )
            except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                logger.exception("Failed to publish index generation")
                return GenerationBuildResult(
                    published=False,
                    error=str(exc),
                    stage=STAGE_PUBLICATION,
                    graph_stats=graph_stats,
                )

            staging.published = True

        self._adopt_generation(published)
        return GenerationBuildResult(
            published=True,
            generation_id=published.generation_id,
            kind=published.kind,
            chunk_count=chunk_count,
            error=str(semantic_error) if semantic_error is not None else None,
            stage=STAGE_SEMANTIC_INDEX if semantic_error is not None else None,
            graph_stats=graph_stats,
        )

    def _write_staged_knowledge_store(
        self, staging_dir: Path, builder: GraphBuilder
    ) -> None:
        """Write ``knowledge.db`` into a staging generation and close it."""
        store = SqliteKnowledgeStore(staging_dir / "knowledge.db")
        try:
            # bulk_insert owns its connection, lock, and transaction (ADR 2):
            # callers must not drive a manual BEGIN/COMMIT around individually
            # locking methods.
            store.bulk_insert(
                entities=list(builder.entities.values()),
                relationships=list(builder.relationships),
            )
        finally:
            store.close()

    def _seed_staging_from(
        self, previous: ResolvedGeneration, staging_dir: Path
    ) -> None:
        """Copy a previous generation's semantic artifacts into staging.

        Incremental builds start from the last published generation instead of
        mutating it. Step 15 replaces this full copy with a validated
        copy-on-write delta; until then correctness outranks the copy cost,
        which is documented alongside retention.
        """
        for name in ("chunks.db", "index_manifest.json", "vectors.json"):
            source = previous.path / name
            if source.exists():
                shutil.copy2(source, staging_dir / name)
        for name in generations.NATIVE_VECTOR_ARTIFACTS:
            source = previous.path / name
            if source.is_dir():
                shutil.copytree(source, staging_dir / name)
            elif source.exists():
                shutil.copy2(source, staging_dir / name)

    @staticmethod
    def _discard_semantic_artifacts(staging_dir: Path) -> None:
        """Remove partial semantic artifacts from a graph-only generation."""
        for name in ("chunks.db", "index_manifest.json", "vectors.json"):
            for path in staging_dir.glob(f"{name}*"):
                path.unlink(missing_ok=True)
        for name in generations.NATIVE_VECTOR_ARTIFACTS:
            path = staging_dir / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()

    @staticmethod
    def _assert_chunk_vector_parity(chunk_ids: list[str], vector_count: int) -> None:
        """Fail a staged generation whose chunk and vector membership disagree."""
        if len(chunk_ids) != vector_count:
            raise generations.GenerationValidationError(
                Path("<staged>"),
                [
                    "chunk/vector count mismatch before publication: "
                    f"chunks={len(chunk_ids)} vectors={vector_count}"
                ],
            )

    @staticmethod
    def _embedding_metadata(provider: Any) -> dict[str, Any]:
        """Serialize the embedding configuration recorded in a generation."""
        from dataclasses import asdict

        return dict(asdict(provider.config))

    def _adopt_generation(self, published: ResolvedGeneration) -> None:
        """Point this service at a generation it just published."""
        self._generation = published
        self._generation_resolved = True
        self._store = None
        self._indexer = None
        self._search_engine = None

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

        The knowledge graph is no longer committed before the semantic index is
        proven. Both are staged into one generation and published together, so
        a failed rebuild leaves the previous generation searchable instead of
        replacing it with a graph that has no index (Step 14).

        Args:
            directory: Root directory to scan and parse.
            output: Destination path for the knowledge store JSON.
            ignore: Additional ignore patterns.
            temporal: Whether to include git history analysis.
            coverage: Optional Cobertura coverage report path.
            incremental: Whether to use incremental index build.

        Returns:
            Statistics from the graph builder plus the publication outcome:
            ``published``, ``generation_id``, ``indexed_chunks``, and — when
            something failed — ``index_error`` and ``index_error_stage``.
        """
        builder = GraphBuilder()
        builder.build_from_directory(
            root_dir=directory,
            additional_ignores=ignore,
            analyze_temporal=temporal,
            coverage_path=Path(coverage) if coverage else None,
        )

        output_path = Path(output)
        store_root = output_path if output_path.is_dir() else output_path.parent
        index_path = store_root / "knowcode_index"

        if export_json:
            # The legacy JSON exporter is deliberately outside the generation:
            # ADR 7 allows reading it, never mixing it into a published one.
            store = KnowledgeStore.from_graph_builder(builder)
            json_path = output_path / KnowledgeStore.DEFAULT_FILENAME if output_path.is_dir() else output_path.with_suffix(".json")
            store.save(json_path)

        result = self.build_generation(
            Path(directory),
            index_path,
            incremental=incremental,
            builder=builder,
        )

        stats: dict[str, Any] = builder.stats()
        stats["indexed_chunks"] = result.chunk_count
        stats["index_path"] = str(index_path)
        stats["published"] = result.published
        stats["generation_id"] = result.generation_id
        stats["generation_kind"] = result.kind
        stats["store_path"] = str(self._store_file())
        if result.error:
            stats["index_error"] = result.error
            stats["index_error_stage"] = result.stage
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

        # SqliteKnowledgeStore defines an ``entities`` property, so the
        # ``hasattr`` branch below is True for it and would hydrate every row
        # into Python just to count them. Dispatch on the capability first so
        # the SQLite path uses server-side GROUP BY and never materializes.
        if isinstance(self.store, SqliteKnowledgeStore):
            counts = self.store.count_by_kind()
            by_kind = counts["entities"]
            rel_types = counts["relationships"]
            total_entities = sum(by_kind.values())
            total_relationships = sum(rel_types.values())
        elif hasattr(self.store, "entities"):
            total_entities = len(self.store.entities)
            for entity in self.store.entities.values():
                kind = entity.kind.value
                by_kind[kind] = by_kind.get(kind, 0) + 1

            total_relationships = len(self.store.relationships)
            for rel in self.store.relationships:
                kind = rel.kind.value
                rel_types[kind] = rel_types.get(kind, 0) + 1

        stats = {
            "total_entities": total_entities,
            "entities_by_kind": by_kind,
            "total_relationships": total_relationships,
            "relationships_by_type": rel_types,
        }

        # Add index stats if indexer is loaded
        if self._indexer:
            stats["total_chunks"] = self._indexer.chunk_repo.count()
            # ``count()`` is the protocol's live-vector count, so both backends
            # report it; the old ``index.ntotal`` probe silently omitted the
            # field for LanceDB and counted FAISS tombstones before Step 11.
            stats["vector_index_size"] = self._indexer.vector_store.count()

        generation = self.current_generation()
        if generation is not None:
            stats["generation_id"] = generation.generation_id
            stats["generation_kind"] = generation.kind

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
        """Re-resolve the current generation and drop every derived component.

        The store, chunk repository, vector store, and search engine are all
        released together so a reload can never leave one of them on an older
        generation than the others. Retired resources are dropped rather than
        closed: an in-flight reader may still hold them, and the reference
        counting that makes closing safe is Step 18.
        """
        self._store = None
        self._indexer = None
        self._search_engine = None
        self._generation = None
        self._generation_resolved = False
        try:
            # Force reload by accessing the property
            _ = self.store
        except FileNotFoundError as e:
            # If the file is gone, keep _store as None
            logger.warning("Knowledge store file not found during reload: %s", e)
        except MissingKnowledgeStoreError as e:
            logger.warning("Knowledge store missing during reload: %s", e)

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
