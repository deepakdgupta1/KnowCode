"""The watch worker's writer handle onto the current generation (Step 18).

``ServerResources.startup()`` used to build the background worker with one
``service.get_indexer()`` result and hold it for the process's lifetime. That
binding was already wrong before this step — after a ``reload()`` the worker
kept committing into the generation the service had moved off — and Step 18
makes it unsafe as well, because retirement now *closes* a superseded
generation's chunk repository and vector store once its last reader releases.

This handle resolves the current bundle per commit, under a lease. Two things
follow from that:

* A commit always lands in the generation the service is serving, so a watched
  edit is visible to the next query rather than to nobody.
* The lease keeps that generation's resources open for the whole transaction,
  so a reload racing a commit cannot close a repository mid-transaction. The
  swap simply takes effect from the next commit.

It deliberately exposes only the two operations the worker calls. A watch
worker has no business reading the graph or driving a rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowcode.service import KnowCodeService


class ServiceWatchWriter:
    """Commits watched file updates into whichever generation is current."""

    def __init__(self, service: "KnowCodeService") -> None:
        """Initialize the handle.

        Args:
            service: Service whose current generation each commit resolves
                against.
        """
        self._service = service

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ServiceWatchWriter(store_path={self._service.store_path!r})"

    def replace_file(self, file_path: str | Path) -> Any:
        """Replace one file's chunks and vectors in the current generation."""
        with self._service.generation_lease() as bundle:
            return bundle.indexer.replace_file(file_path)

    def delete_file(self, file_path: str | Path) -> Any:
        """Remove one file's chunks and vectors from the current generation."""
        with self._service.generation_lease() as bundle:
            return bundle.indexer.delete_file(file_path)
