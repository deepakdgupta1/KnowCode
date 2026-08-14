"""Service layer for KnowCode business logic."""

from __future__ import annotations

import os
import re
import shutil
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional, cast

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.config import AppConfig
from knowcode.errors import (
    MissingKnowledgeStoreError,
    MissingSemanticIndexError,
    RepositoryClosedError,
)
from knowcode.generation_bundle import BundleSources, GenerationBundle
from knowcode.indexing import generations
from knowcode.indexing.generations import ResolvedGeneration
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.service_watch import ServiceWatchWriter
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

#: Bundles pinned by the ``generation_lease()`` calls active on this execution
#: context, innermost last. A :class:`~contextvars.ContextVar` rather than a
#: thread-local so one lease covers nested calls in both a sync request thread
#: and an async context — and so a thread started inside a lease does not
#: inherit a pin it would never release. Entries are keyed by service instance
#: because two services can legitimately be leased at once in one request.
_LEASE_STACK: ContextVar[tuple[tuple["KnowCodeService", GenerationBundle], ...]] = (
    ContextVar("knowcode_lease_stack", default=())
)

#: Bound on re-resolving the current bundle when it retires between resolution
#: and acquisition. Each retry follows a completed swap, so more than a couple
#: means a bug rather than contention; the cap turns that into a clear error
#: instead of a spinning request thread.
_LEASE_ACQUIRE_ATTEMPTS = 64


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
        # One generation at a time (Step 18). The bundle replaces the three
        # unlocked lazy fields this service used to keep — store, indexer,
        # search engine — which two threads could each build and a reload could
        # replace one at a time underneath an in-flight request.
        self._bundle: Optional[GenerationBundle] = None
        self._retired_bundles: list[GenerationBundle] = []
        self._bundle_lock = threading.RLock()
        # Held for a whole reload so two of them cannot both build a candidate
        # and publish in an order neither chose. Lock order is always
        # ``_reload_lock`` then ``_bundle_lock``; nothing acquires the reverse.
        self._reload_lock = threading.Lock()
        self._closed = False
        self._retrieval_orchestrator = RetrievalOrchestrator(self)

    # ------------------------------------------------------------------
    # Generation bundles and reader leases (Step 18, ADR 4)
    # ------------------------------------------------------------------

    @contextmanager
    def generation_lease(self) -> Iterator[GenerationBundle]:
        """Pin one complete generation for the duration of an operation.

        Everything this service hands out while the lease is held — the
        knowledge store, the indexer, the search engine — comes from the same
        published generation, even if a rebuild or ``reload()`` publishes a
        newer one meanwhile. The pinned generation's SQLite and vector
        resources stay open until the lease ends, so a retirement can never
        close a connection out from under a request.

        Nesting reuses the lease already held by this service on this execution
        context rather than taking a second one, so a service method that
        leases internally never shadows the caller that leased around it.

        Raises:
            RepositoryClosedError: The service has been closed.
        """
        existing = self._leased_bundle()
        if existing is not None:
            yield existing
            return

        bundle = self._acquire_bundle()
        token = _LEASE_STACK.set(_LEASE_STACK.get() + ((self, bundle),))
        try:
            yield bundle
        finally:
            _LEASE_STACK.reset(token)
            bundle.release()

    def live_generation_ids(self) -> tuple[str, ...]:
        """Generation ids this service still holds open, newest first.

        The current bundle plus every retired bundle whose last reader has not
        released yet. Publication passes these to retention so a generation
        directory is never removed while a request is still reading it (ADR 4:
        "superseded generations are retired only after reader leases end").
        """
        self._reap_retired()
        with self._bundle_lock:
            bundles = [self._bundle] if self._bundle is not None else []
            bundles.extend(self._retired_bundles)
        ids = [
            bundle.generation_id
            for bundle in bundles
            if bundle.generation_id is not None
        ]
        return tuple(dict.fromkeys(ids))

    def watch_writer(self) -> ServiceWatchWriter:
        """Return a writer that commits into whatever generation is current.

        The watch worker used to be handed one :class:`~knowcode.indexing.
        indexer.Indexer` at startup and hold it forever, so after a reload it
        kept committing into the retired generation — and, once retirement
        started closing resources, into a closed repository. This handle
        resolves the current bundle under a lease per commit instead.
        """
        return ServiceWatchWriter(self)

    def _leased_bundle(self) -> Optional[GenerationBundle]:
        """The bundle this service has pinned on this execution context."""
        for service, bundle in reversed(_LEASE_STACK.get()):
            if service is self:
                return bundle
        return None

    def _acquire_bundle(self) -> GenerationBundle:
        """Resolve the current bundle and take a lease on it."""
        for _ in range(_LEASE_ACQUIRE_ATTEMPTS):
            bundle = self._live_bundle()
            if bundle.acquire():
                return bundle
            # The bundle retired between resolution and acquisition, which
            # means a swap completed in between: resolve the new one.
        raise RuntimeError(
            "Could not acquire a stable index generation after "
            f"{_LEASE_ACQUIRE_ATTEMPTS} attempts"
        )

    def _live_bundle(
        self, index_path: Optional[str | Path] = None
    ) -> GenerationBundle:
        """The bundle new operations start from, built once on first use.

        Single-flight: the lock covers the check *and* the construction, so
        concurrent first use cannot build two bundles. Opening the components
        inside a bundle is separately single-flighted by the bundle itself, so
        this lock is held only for the cheap object construction.
        """
        with self._bundle_lock:
            self._assert_open()
            if self._bundle is None:
                self._bundle = self._open_bundle(index_path=index_path)
            return self._bundle

    def _current_bundle(
        self, index_path: Optional[str | Path] = None
    ) -> GenerationBundle:
        """The bundle this caller must read from: its lease, or the live one."""
        leased = self._leased_bundle()
        if leased is not None:
            return leased
        return self._live_bundle(index_path)

    def _open_bundle(
        self,
        *,
        generation: Optional[ResolvedGeneration] = None,
        index_path: Optional[str | Path] = None,
    ) -> GenerationBundle:
        """Build an unopened bundle for one generation.

        Nothing is opened here: a reload candidate that is rejected, or a
        service that only answers ``/health``, must not pay for a SQLite
        connection.
        """
        resolved = (
            generation
            if generation is not None
            else generations.resolve_current_generation(self._index_path())
        )
        return GenerationBundle(
            generation=resolved,
            sources=BundleSources(
                open_store=lambda: self._open_store(resolved),
                open_indexer=lambda: self._open_indexer(resolved, index_path),
                open_search_engine=self._open_search_engine,
            ),
        )

    def _open_store(self, generation: Optional[ResolvedGeneration]) -> Any:
        """Open one generation's knowledge store."""
        store_file = self._store_file_for(generation)
        if not store_file.exists():
            raise MissingKnowledgeStoreError(store_file)
        if store_file.suffix == ".db":
            return SqliteKnowledgeStore(store_file)
        return KnowledgeStore.load(store_file)

    def _open_indexer(
        self,
        generation: Optional[ResolvedGeneration],
        index_path: Optional[str | Path] = None,
    ) -> "Indexer":
        """Open one generation's chunk repository and vector store."""
        provider = create_embedding_provider(app_config=self.app_config)
        artifact_dir = self._artifact_dir_for(generation, index_path)
        chunk_repo = SqliteChunkRepository(artifact_dir / "chunks.db")
        vector_store = create_vector_store(
            self.app_config.vector_backend,
            dimension=provider.config.dimension,
            index_dir=artifact_dir,
        )
        indexer = Indexer(provider, chunk_repo=chunk_repo, vector_store=vector_store)
        if artifact_dir.exists():
            indexer.load(artifact_dir)
        return indexer

    def _open_search_engine(self, store: Any, indexer: "Indexer") -> "SearchEngine":
        """Build the search engine over one generation's store and indexer."""
        hybrid_index = HybridIndex(
            indexer.chunk_repo,
            indexer.vector_store,
            alpha=self.app_config.hybrid_alpha,
        )
        return SearchEngine(
            indexer.chunk_repo,
            indexer.embedding_provider,
            hybrid_index,
            store,
            config=self.app_config,
        )

    def _swap(self, candidate: GenerationBundle) -> None:
        """Install ``candidate`` as current and retire what it replaces.

        The swap itself is one assignment under a narrow lock. Retirement — and
        therefore closing — happens outside it, because closing a store waits
        for that store's own in-flight readers to drain (Step 09) and holding
        the bundle lock across that would stall every unrelated request.

        Raises:
            RepositoryClosedError: The service was closed while the candidate
                was being built. The candidate is closed rather than installed.
        """
        with self._bundle_lock:
            if self._closed:
                candidate.retire()
                raise RepositoryClosedError(
                    "This KnowCodeService was closed while a new index "
                    "generation was being loaded."
                )
            previous, self._bundle = self._bundle, candidate
            if previous is not None:
                self._retired_bundles.append(previous)

        if previous is not None:
            previous.retire()
        self._reap_retired()

    def _reap_retired(self) -> None:
        """Forget retired bundles whose last reader has released."""
        with self._bundle_lock:
            self._retired_bundles = [
                bundle for bundle in self._retired_bundles if not bundle.is_closed
            ]

    def _assert_open(self) -> None:
        """Raise if this service has been closed."""
        if self._closed:
            raise RepositoryClosedError(
                "This KnowCodeService is closed; construct a new one."
            )

    @property
    def store(self) -> Any:
        """The knowledge store of the generation this caller is reading."""
        return self._current_bundle().store

    def _store_root(self) -> Path:
        """Resolve the root directory where store/index artifacts live."""
        return self.store_path if self.store_path.is_dir() else self.store_path.parent

    # ------------------------------------------------------------------
    # Index generations (Step 14, ADR 4)
    # ------------------------------------------------------------------

    def current_generation(self) -> Optional[ResolvedGeneration]:
        """The published generation this caller is reading from.

        Resolved once per bundle so the knowledge store, chunk repository,
        vector store, and search engine handed out together all come from one
        generation. Inside a :meth:`generation_lease` this is the leased
        generation and cannot change; outside one it is whatever bundle is
        current at the moment of the call.
        """
        return self._current_bundle().generation

    def _artifact_dir_for(
        self,
        generation: Optional[ResolvedGeneration],
        index_path: Optional[str | Path] = None,
    ) -> Path:
        """Directory holding the chunk/vector artifacts a reader should open.

        A published generation with a semantic index owns its own directory.
        Otherwise this is the flat index root, which is both the pre-Step-14
        layout and where a not-yet-built index would go. A graph-only
        generation deliberately does not route here: opening a chunk store
        inside it would create ``chunks.db`` in an immutable generation.
        """
        if index_path is not None:
            root = Path(index_path)
            resolved = generations.resolve_current_generation(root)
            if resolved is not None and resolved.has_semantic_index:
                return resolved.path
            return root

        if generation is not None and generation.has_semantic_index:
            return generation.path
        return self._index_path()

    def _store_file_for(self, generation: Optional[ResolvedGeneration]) -> Path:
        """Resolve the knowledge store file path for one generation.

        A published generation owns the authoritative ``knowledge.db``. Without
        one, the legacy flat layout is used so pre-Step-14 installs stay
        readable.
        """
        if generation is not None:
            return generation.knowledge_db

        if self.store_path.is_dir():
            db_path = self.store_path / "knowledge.db"
            if db_path.exists():
                return db_path
            return self.store_path / KnowledgeStore.DEFAULT_FILENAME
        return self.store_path

    def _store_file(self) -> Path:
        """Knowledge store path for the generation this caller is reading."""
        return self._store_file_for(self.current_generation())

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
        """The indexer of the generation this caller is reading.

        Args:
            index_path: Optional artifact root override. As before, it applies
                only when this service opens its first bundle; once a
                generation is bound, its own artifacts are authoritative.

        Returns:
            The bundle's Indexer, opened at most once.
        """
        return cast("Indexer", self._current_bundle(index_path).indexer)

    def get_search_engine(
        self, index_path: Optional[str | Path] = None
    ) -> "SearchEngine":
        """The search engine of the generation this caller is reading.

        Args:
            index_path: Optional artifact root override, as for
                :meth:`get_indexer`.

        Returns:
            SearchEngine wired to this generation's store, chunks, and vectors.
        """
        return cast("SearchEngine", self._current_bundle(index_path).search_engine)

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
        # One lease for the whole bundle: freshness, retrieval, and the
        # chunks the caller resolves afterwards must all describe one
        # generation, or a rebuild mid-query answers half from each.
        with self.generation_lease():
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
                    index_root,
                    staging.path,
                    manifest,
                    # A generation a request is still reading must survive
                    # retention until that reader releases its lease (Step 18).
                    protect=self.live_generation_ids(),
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
        """Move this service onto a generation it just published.

        One atomic swap of one complete bundle, so a build can never leave the
        graph on the new generation while chunks and vectors are still on the
        old one. The replaced bundle retires behind whatever readers still hold
        it.
        """
        self._swap(self._open_bundle(generation=published))

    # ------------------------------------------------------------------
    # Resource ownership (Step 17)
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Make this service's in-memory index state durable.

        Two things are durable in different ways, so both are handled:

        * The vector store's write buffer is drained into its table. LanceDB
          buffers; FAISS/NumPy does not, and its flush is a documented no-op.
        * The vector *artifact* and the index manifest are written back to the
          artifact directory. Step 15 commits keep chunks durable in SQLite as
          they go, but with the FAISS backend the vectors live only in memory
          until someone calls :meth:`Indexer.save`. A watch session that ended
          without this left one vector in memory and none on disk, so a restart
          found durable chunks and no vectors.

        A *published* generation is deliberately left untouched. ADR 4 makes it
        immutable and records artifact checksums at publication, so rewriting
        ``vectors.*`` or ``index_manifest.json`` inside it would invalidate the
        manifest that describes it. Watch commits against a published
        generation are therefore not made durable here; routing them through a
        staged generation is Step 18b's, and this method says so rather than
        pretending the write happened.
        """
        bundle = self._bundle
        if bundle is None or not bundle.has_indexer:
            # Nothing was opened, so nothing is buffered. Opening an indexer
            # here just to flush it would create artifacts during shutdown.
            return
        indexer = bundle.indexer

        indexer.vector_store.flush()

        generation = bundle.generation
        artifact_dir = self._artifact_dir_for(generation)
        if generation is not None and artifact_dir == generation.path:
            logger.info(
                "Skipping the index artifact flush: generation %s is published "
                "and immutable (ADR 4).",
                generation.generation_id,
            )
            return
        if not self._has_index_state(indexer, artifact_dir):
            # Nothing was ever indexed here, so there is nothing to make
            # durable. Writing anyway would publish a metadata envelope
            # describing an index that does not exist, which fails closed on
            # the next load — an empty LanceDB save produces exactly that.
            return
        indexer.save(artifact_dir)

    @staticmethod
    def _has_index_state(indexer: "Indexer", artifact_dir: Path) -> bool:
        """Whether flushing this indexer would persist anything real.

        An empty store beside a previously saved artifact is still worth
        writing: it records that everything was removed. An empty store with no
        artifact is simply an index nobody built.
        """
        if indexer.vector_store.count() > 0:
            return True
        return (artifact_dir / "vectors.json").exists()

    def close(self) -> None:
        """Retire every generation this service opened. Idempotent.

        New leases are refused immediately, and each bundle closes its own
        components in reverse order of dependency (Step 18). A bundle that a
        request still holds closes when that request releases it, so shutdown
        never raises inside an operation that was already running; with no
        readers in flight — the normal shutdown case, since the server has
        stopped accepting requests — every store closes here.
        """
        with self._bundle_lock:
            if self._closed:
                return
            self._closed = True
            bundle, self._bundle = self._bundle, None
            if bundle is not None:
                self._retired_bundles.append(bundle)
            retiring = list(self._retired_bundles)

        for retired in retiring:
            retired.retire()
        self._reap_retired()

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`close` has stopped this service handing out leases."""
        return self._closed

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
        with self.generation_lease() as bundle:
            entities = bundle.store.search(pattern)
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
        with self.generation_lease():
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
        with self.generation_lease():
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

            # Index stats only when this generation's indexer is already open;
            # reporting statistics must not open a chunk repository as a side
            # effect, least of all inside an immutable published generation.
            bundle = self._current_bundle()
            if bundle.has_indexer:
                indexer = bundle.indexer
                stats["total_chunks"] = indexer.chunk_repo.count()
                # ``count()`` is the protocol's live-vector count, so both backends
                # report it; the old ``index.ntotal`` probe silently omitted the
                # field for LanceDB and counted FAISS tombstones before Step 11.
                stats["vector_index_size"] = indexer.vector_store.count()

            generation = bundle.generation
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
        with self.generation_lease() as bundle:
            callers = bundle.store.get_callers(entity_id)
        return [
            {"id": c.id, "name": c.qualified_name, "file": c.location.file_path}
            for c in callers
        ]

    def reload(self) -> bool:
        """Move onto the newest published generation, if there is one.

        The replacement bundle is built and warmed *off to the side* first, so
        a generation that cannot be loaded is rejected while the current one is
        still serving. Only a bundle that opened successfully is swapped in,
        and it is swapped in whole: the store, chunk repository, vector store,
        and search engine can never end up on different generations.

        Before Step 18 this reset three lazy fields and re-read the store. A
        reload whose new generation could not be loaded therefore left the
        service with *no* store at all — a service answering queries became
        permanently unusable after one failed refresh — and the components it
        dropped were never closed, leaking two SQLite connections per reload.

        Returns:
            Whether this call moved the service onto a different generation.
            ``False`` means nothing changed: either the pointer still names the
            generation already in use, or the replacement could not be loaded
            and the current one was kept.

        Raises:
            RepositoryClosedError: The service has been closed.
        """
        with self._reload_lock:
            self._assert_open()
            previous = self._bundle
            resolved = generations.resolve_current_generation(self._index_path())

            if previous is not None and self._is_same_generation(
                previous.generation, resolved
            ):
                return False

            candidate = self._open_bundle(generation=resolved)
            try:
                candidate.warm(
                    store=True,
                    indexer=previous is not None and previous.has_indexer,
                    search_engine=previous is not None
                    and previous.has_search_engine,
                )
            except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                candidate.close()
                logger.warning(
                    "Keeping index generation %s: the replacement could not be "
                    "loaded: %s",
                    previous.generation_id if previous is not None else "<none>",
                    exc,
                )
                return False

            self._swap(candidate)
            return True

    @staticmethod
    def _is_same_generation(
        current: Optional[ResolvedGeneration],
        candidate: Optional[ResolvedGeneration],
    ) -> bool:
        """Whether a reload would land on the generation already in use.

        A missing generation on either side is treated as *different*: the
        pre-Step-14 flat layout has no generation identity, so a reload there
        must genuinely reopen its artifacts rather than assume they are the
        ones already held.
        """
        if current is None or candidate is None:
            return False
        return current.generation_id == candidate.generation_id

    def get_entity_details(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Get detailed information about an entity as a dictionary.

        This returns the raw structured data including source code,
        docstrings, and metadata, which is useful for tool-calling agents.
        """
        with self.generation_lease():
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
        with self.generation_lease() as bundle:
            callees = bundle.store.get_callees(entity_id)
        return [{"id": c.id, "name": c.qualified_name} for c in callees]
