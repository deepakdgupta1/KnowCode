"""The watch worker's writer handle onto the index (Steps 18 and 18b).

``ServerResources.startup()`` used to build the background worker with one
``service.get_indexer()`` result and hold it for the process's lifetime. That
binding was already wrong before Step 18 — after a ``reload()`` the worker kept
committing into the generation the service had moved off — and Step 18 made it
unsafe as well, because retirement now *closes* a superseded generation's chunk
repository and vector store once its last reader releases.

Step 18 fixed *which* generation a commit reached; Step 18b fixes *what a
commit is allowed to write*. Resolving the current bundle per commit still
handed the worker the published generation's own ``chunks.db`` and vector
artifacts, and ADR 4 makes those immutable: one watched edit left
``validate_generation(..., verify_digests=True)`` reporting a chunk-id digest
mismatch and a chunk-count mismatch, and ``knowcode doctor`` failing. On
FAISS/NumPy the edit was not even durable, because ``KnowCodeService.flush()``
correctly refuses to rewrite ``vectors.*`` inside a published generation.

So a watch commit is now a *generation* like every other index mutation:

* **Staged.** The first commit of a batch copies the current generation into a
  staging directory no reader can resolve, and every commit in that batch is a
  Step 15 transaction against the copy.
* **Published on a drain.** The worker publishes when its queue goes idle —
  one burst of saves, one staging copy, one publication — and the batch cap
  publishes mid-burst so a thousand-file checkout still bounds both reader
  staleness and the work a crash would discard. Readers move across the whole
  batch in one atomic swap (Step 18).
* **Rebased, never reverted.** A staged batch is a successor to the generation
  it was seeded from. If a build publishes in between, publication refuses and
  the batch is re-derived on the new current generation instead of overwriting
  it.
* **Never durable by implication.** Work still in staging is named by
  :meth:`ServiceWatchWriter.pending_paths`, which is what the worker's drain
  report and the server's shutdown report carry.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from knowcode.indexing import generations
from knowcode.indexing.generation_writer import StagedGenerationWriter
from knowcode.utils.entity_identity import normalize_file_identity
from knowcode.utils.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowcode.service import KnowCodeService

logger = get_logger(__name__)

#: Commits one staging generation accumulates before publishing itself.
#: Publication normally happens when the watch queue goes idle, so this only
#: bites during a sustained burst — a branch switch, a bulk format. It bounds
#: two things at once: how stale a reader can get while the burst runs, and how
#: much committed work a crash would leave unpublished.
DEFAULT_WATCH_BATCH_COMMITS = 64

#: How many times a publication re-derives its batch after losing the pointer
#: to another writer. One retry covers the ordinary build-lands-mid-batch race;
#: a second conflict means sustained contention, which is reported rather than
#: retried forever.
_REBASE_ATTEMPTS = 2

_REPLACE = "replace_file"
_DELETE = "delete_file"


class ServiceWatchWriter:
    """Commits watched file updates, and publishes them as generations."""

    def __init__(
        self,
        service: "KnowCodeService",
        *,
        max_batch_commits: int = DEFAULT_WATCH_BATCH_COMMITS,
    ) -> None:
        """Initialize the handle.

        Args:
            service: Service whose current generation each batch succeeds.
            max_batch_commits: Commits one staging generation accumulates
                before it publishes itself.
        """
        if max_batch_commits < 1:
            raise ValueError("max_batch_commits must be at least 1")
        self._service = service
        self.max_batch_commits = max_batch_commits
        # One batch at a time. Held across a whole commit and a whole
        # publication, so a commit can never land in a staging directory that
        # publication has already renamed away.
        self._lock = threading.RLock()
        self._session: Optional[StagedGenerationWriter] = None
        #: The batch's transactions in order, kept until they are published so
        #: they can be re-derived on a newer generation after a conflict.
        self._operations: list[tuple[str, str]] = []

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ServiceWatchWriter(store_path={self._service.store_path!r}, "
            f"staged={len(self._operations)})"
        )

    # -- transactions ----------------------------------------------------

    def replace_file(self, file_path: str | Path) -> Any:
        """Replace one file's chunks and vectors in the next generation."""
        return self._commit(_REPLACE, file_path)

    def delete_file(self, file_path: str | Path) -> Any:
        """Remove one file's chunks and vectors from the next generation."""
        return self._commit(_DELETE, file_path)

    # -- publication -----------------------------------------------------

    def publish_pending(self) -> Optional[str]:
        """Publish the staged batch and move readers onto it.

        Returns:
            The published generation id, or ``None`` when nothing was staged.

        Raises:
            Exception: Publication failed. The staged directory is gone, but
                the batch's transactions are retained, so the next call
                re-derives and republishes them and :meth:`pending_paths` keeps
                naming them as work that is not in the index.
        """
        with self._lock:
            return self._publish_locked()

    def pending_paths(self) -> tuple[str, ...]:
        """Committed-but-unpublished file identities, in commit order.

        This is the honest answer to "what did the index not get", and it is
        what the worker's drain report and the server's shutdown report carry.
        """
        with self._lock:
            return tuple(dict.fromkeys(path for _, path in self._operations))

    @property
    def has_pending(self) -> bool:
        """Whether any committed work is still unpublished."""
        with self._lock:
            return bool(self._operations)

    def close(self) -> tuple[str, ...]:
        """Discard the staged batch without publishing it.

        Returns:
            The identities whose updates are lost, so the caller can report
            them rather than let a clean-looking shutdown imply durability.
        """
        with self._lock:
            lost = self.pending_paths()
            session, self._session = self._session, None
            self._operations = []
        if session is not None:
            session.discard()
        if lost:
            logger.warning(
                "Discarding %d unpublished watched update(s): %s",
                len(lost),
                ", ".join(lost),
            )
        return lost

    # -- internals -------------------------------------------------------

    def _commit(self, operation: str, file_path: str | Path) -> Any:
        """Apply one transaction to the staged batch, publishing at the cap."""
        with self._lock:
            session = self._open_session_locked()
            if session is None:
                # No published generation: the flat index root is the live,
                # mutable index (the pre-Step-14 layout, and an index nobody
                # has built yet). There is nothing immutable to protect and
                # ``flush()`` already makes it durable, so commit in place.
                return self._commit_in_place(operation, file_path)

            result = getattr(session, operation)(file_path)
            self._operations.append((operation, normalize_file_identity(file_path)))
            if len(self._operations) >= self.max_batch_commits:
                self._publish_locked()
            return result

    def _commit_in_place(self, operation: str, file_path: str | Path) -> Any:
        """Commit into the live flat index, under a lease (Step 18)."""
        with self._service.generation_lease() as bundle:
            return getattr(bundle.indexer, operation)(file_path)

    def _open_session_locked(self) -> Optional[StagedGenerationWriter]:
        """The batch's staging generation, seeded on first use.

        Returns ``None`` when there is no published generation with a semantic
        index to succeed.
        """
        if self._session is not None:
            return self._session

        # Under a lease so the base generation's directory cannot be retired
        # out from under the copy.
        with self._service.generation_lease() as bundle:
            base = bundle.generation
            if base is None or not base.has_semantic_index:
                return None
            self._session = StagedGenerationWriter(
                index_root=self._service.index_root,
                base=base,
                provider=bundle.indexer.embedding_provider,
                backend=self._service.app_config.vector_backend,
            )
        return self._session

    def _publish_locked(self) -> Optional[str]:
        """Publish the batch, re-deriving it once if the pointer moved."""
        for attempt in range(_REBASE_ATTEMPTS):
            if self._session is None and self._operations:
                # A previous publication conflicted or failed: rebuild the batch
                # against whatever generation is current now.
                self._restage_locked()
            if self._session is None:
                return None
            try:
                return self._publish_session_locked()
            except generations.GenerationConflictError as exc:
                if attempt == _REBASE_ATTEMPTS - 1:
                    raise
                logger.info(
                    "Re-deriving %d watched update(s) on the current generation: %s",
                    len(self._operations),
                    exc,
                )
        return None  # pragma: no cover - the loop either returns or raises

    def _publish_session_locked(self) -> str:
        """Publish the open session and swap the service onto it.

        The session is detached first: whether publication succeeds or fails,
        its staging directory is gone afterwards. ``_operations`` is cleared
        only on success, so a failure leaves the batch reportable and
        re-derivable.
        """
        session, self._session = self._session, None
        assert session is not None  # guarded by the caller
        published = session.publish(
            protect=self._service.live_generation_ids(),
            expect_current=session.base_generation_id,
        )
        self._operations = []
        self._service.adopt_generation(published)
        logger.info(
            "Published watched updates as index generation %s",
            published.generation_id,
        )
        return published.generation_id

    def _restage_locked(self) -> None:
        """Re-apply this batch's transactions to a fresh staging generation.

        Re-derivation rather than replay of the staged *artifacts*: the batch
        is a list of files whose current content should be in the index, so
        applying it to the new base produces the same intent — with each file's
        newest content — instead of overwriting whatever published in between.
        """
        operations, self._operations = list(self._operations), []
        for index, (operation, file_path) in enumerate(operations):
            try:
                session = self._open_session_locked()
                if session is None:
                    # No generation to succeed any more: the flat index root is
                    # the live one, and a commit there is durable and visible
                    # without publication.
                    self._commit_in_place(operation, file_path)
                    continue
                getattr(session, operation)(file_path)
            except BaseException:
                # Everything not re-derived yet stays on the record, so a
                # failure here leaves the batch reportable and re-derivable
                # instead of quietly dropping it.
                self._operations.extend(operations[index:])
                raise
            self._operations.append((operation, file_path))
