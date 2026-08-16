"""One generation's readers, held together by a reference-counted lease.

Step 18 of the hardening blueprint, completing ADR 4's reader contract:
"Readers acquire one generation lease and use it for sparse, dense, graph, and
context work. Superseded generations are retired only after reader leases end."

Before this module, :class:`~knowcode.service.KnowCodeService` kept the
knowledge store, indexer, and search engine in three independent, unlocked
lazy fields. Three defects followed directly from that shape:

* Two threads racing first use each ran the ``if self._store is None`` check
  and built their own :class:`~knowcode.storage.sqlite_knowledge_store.
  SqliteKnowledgeStore`. Reproduced: four concurrent readers opened four stores.
* ``reload()`` replaced those fields while a request was mid-flight, so one
  operation could answer its graph half from one generation and its dense half
  from the next.
* Retired stores were dropped rather than closed — one leaked writer connection
  and one leaked reader connection per reload — because closing them would have
  torn a connection out from under whichever request still held it.

A :class:`GenerationBundle` fixes the shape rather than each symptom. It is the
immutable set of components for exactly one published generation:

* **Single-flight materialization.** Components open lazily (a bundle nobody
  reads must not open a SQLite connection) but at most once. A failed open
  caches nothing, so the next caller retries against a bundle that is still
  whole rather than a half-built one.
* **Reference-counted leases.** :meth:`GenerationBundle.acquire` pins the whole
  bundle for one operation; :meth:`~GenerationBundle.release` ends it.
* **Retirement behind the last reader.** :meth:`~GenerationBundle.retire` marks
  a bundle superseded and refuses new leases. It closes immediately when no
  reader holds it and otherwise closes the moment the final reader releases —
  so a retired generation is neither leaked nor closed mid-request.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from knowcode.indexing.generations import ResolvedGeneration
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class BundleSources:
    """How to open one generation's components, on demand.

    Callables rather than values: a bundle that is built but never read — a
    reload candidate that is rejected, a service that only answers ``/health``
    — must not pay for a SQLite connection or a LanceDB handle.
    """

    #: Open the knowledge store for this generation.
    open_store: Callable[[], Any]
    #: Open the indexer (chunk repository plus vector store) for this generation.
    open_indexer: Callable[[], Any]
    #: Build the search engine from this generation's store and indexer.
    open_search_engine: Callable[[Any, Any], Any]


class GenerationBundle:
    """The complete, immutable reader set for one index generation."""

    def __init__(
        self,
        generation: Optional[ResolvedGeneration],
        sources: BundleSources,
    ) -> None:
        """Initialize a bundle.

        Args:
            generation: The published generation these components read, or
                ``None`` for the pre-Step-14 flat layout and for an index that
                has not been built yet.
            sources: Callables that open the components on first use.
        """
        self.generation = generation
        self._sources = sources
        # One reentrant lock covers materialization, lease bookkeeping, and
        # teardown. Reentrant because ``search_engine`` materializes ``store``
        # and ``indexer`` first, and because the final ``release()`` closes the
        # bundle from inside the same critical section.
        self._lock = threading.RLock()
        self._store: Any = None
        self._indexer: Any = None
        self._search_engine: Any = None
        self._leases = 0
        self._retired = False
        self._closed = False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"GenerationBundle(generation_id={self.generation_id!r}, "
            f"leases={self.lease_count}, retired={self._retired}, "
            f"closed={self._closed})"
        )

    # -- identity --------------------------------------------------------

    @property
    def generation_id(self) -> Optional[str]:
        """Id of the published generation, or ``None`` for the flat layout."""
        return self.generation.generation_id if self.generation is not None else None

    @property
    def has_semantic_index(self) -> bool:
        """Whether this generation carries a chunk/vector index."""
        return self.generation is not None and self.generation.has_semantic_index

    # -- materialization -------------------------------------------------

    @property
    def store(self) -> Any:
        """This generation's knowledge store, opened at most once."""
        with self._lock:
            if self._store is None:
                self._store = self._sources.open_store()
            return self._store

    @property
    def indexer(self) -> Any:
        """This generation's indexer, opened at most once."""
        with self._lock:
            if self._indexer is None:
                self._indexer = self._sources.open_indexer()
            return self._indexer

    @property
    def search_engine(self) -> Any:
        """This generation's search engine, built at most once."""
        with self._lock:
            if self._search_engine is None:
                self._search_engine = self._sources.open_search_engine(
                    self.store, self.indexer
                )
            return self._search_engine

    @property
    def has_store(self) -> bool:
        """Whether the knowledge store has been opened."""
        return self._store is not None

    @property
    def has_indexer(self) -> bool:
        """Whether the indexer has been opened."""
        return self._indexer is not None

    @property
    def has_search_engine(self) -> bool:
        """Whether the search engine has been built."""
        return self._search_engine is not None

    def warm(
        self,
        *,
        store: bool = False,
        indexer: bool = False,
        search_engine: bool = False,
    ) -> None:
        """Open the requested components now, raising if any of them cannot be.

        A reload builds its replacement bundle *off to the side* and warms it to
        whatever the outgoing bundle had already opened. That is what turns "the
        new generation is unusable" into a rejected candidate instead of a
        service that discovers the problem on its next request — after it has
        already thrown the working generation away.

        The order is deliberate: the knowledge store first, so a generation with
        no readable store is rejected before opening an indexer would create
        ``chunks.db`` next to it.
        """
        if store or indexer or search_engine:
            _ = self.store
        if indexer or search_engine:
            _ = self.indexer
        if search_engine:
            _ = self.search_engine

    # -- leases ----------------------------------------------------------

    def acquire(self) -> bool:
        """Pin this bundle for one operation.

        Returns:
            ``True`` when the lease was granted. ``False`` means the bundle has
            been retired or closed, and the caller must re-resolve the current
            one rather than read a superseded generation.
        """
        with self._lock:
            if self._retired or self._closed:
                return False
            self._leases += 1
            return True

    def release(self) -> None:
        """End one lease, closing the bundle if it was retired and is now idle.

        Raises:
            RuntimeError: More releases than acquisitions. Silently tolerating
                one would let a retired bundle close while a reader still holds
                it, which is the exact failure leases exist to prevent.
        """
        with self._lock:
            if self._leases <= 0:
                raise RuntimeError(
                    "GenerationBundle.release() without a matching acquire()"
                )
            self._leases -= 1
            if self._leases == 0 and self._retired:
                self.close()

    @property
    def lease_count(self) -> int:
        """How many operations currently hold this bundle."""
        with self._lock:
            return self._leases

    def retire(self) -> None:
        """Supersede this bundle: refuse new leases, close when idle.

        Idempotent. A bundle with readers in flight stays open until the last
        one releases, so retirement never closes a connection mid-request.
        """
        with self._lock:
            self._retired = True
            if self._leases == 0:
                self.close()

    @property
    def is_retired(self) -> bool:
        """Whether this bundle has been superseded."""
        with self._lock:
            return self._retired

    # -- teardown --------------------------------------------------------

    def close(self) -> None:
        """Close every component this bundle opened. Idempotent.

        Released in reverse order of dependency — the derived search engine
        first, then the chunk repository and vector store, then the knowledge
        store — so nothing is closed while something built on it is still
        reachable. A component that fails to close does not skip the ones after
        it: losing one connection and leaking the rest is strictly worse than
        losing the one.

        Components that were never opened are not opened just to close them.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._retired = True
            indexer, self._indexer = self._indexer, None
            store, self._store = self._store, None
            self._search_engine = None

        if indexer is not None:
            self._close_quietly(
                getattr(indexer, "chunk_repo", None), "chunk repository"
            )
            self._close_quietly(getattr(indexer, "vector_store", None), "vector store")
        # The JSON knowledge store holds no connection and has no ``close``.
        self._close_quietly(store, "knowledge store")

    @property
    def is_closed(self) -> bool:
        """Whether this bundle's components have been closed."""
        with self._lock:
            return self._closed

    @staticmethod
    def _close_quietly(resource: Any, what: str) -> None:
        """Close one component, reporting rather than propagating a failure."""
        if resource is None:
            return
        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - reported, and the next one still closes
            logger.exception("Closing the retired %s failed", what)
