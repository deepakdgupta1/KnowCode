"""Prepare/commit transactions for one file's chunk generation (Step 15).

Step 14 made a *full* rebuild all-or-nothing. This module does the same for a
single file, which is what watched edits, `index_file`, and incremental builds
actually commit. Before it, every path removed a file's chunks first and hoped
the replacement arrived::

    remove_file(path)          # the previous generation is gone
    index_file(path)           # ... and this is allowed to fail

Reproduced against the pre-Step-15 code with only a failing provider: a file
indexed as ``chunks=2 vectors=2`` became ``chunks=0 vectors=2`` after one failed
re-index, and dense search kept answering with both deleted chunk ids.

The contract:

* **Preparation** reads, parses, chunks, reuses durable embeddings where the
  content is unchanged, embeds the rest, and validates count, dimension, and
  id uniqueness. It mutates nothing. A failure here raises
  :class:`FileUpdatePreparationError` and the live generation is untouched.
* **Commit** replaces the file's whole chunk generation in one SQLite writer
  transaction (:meth:`ChunkRepository.replace_file`) and then applies the
  matching vector upserts and removals. A vector failure *after* the SQLite
  commit is recovered from the durable embeddings the chunk store now holds
  (Step 08); only an unrecoverable failure raises
  :class:`FileUpdateCommitError`, telling the caller its generation is
  inconsistent and must be discarded or rebuilt.

A prepared update is a complete replacement, never a delta: committing it makes
exactly its chunks the file's searchable set, whatever was there before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from knowcode.data_models import CodeChunk


class FileUpdateError(RuntimeError):
    """Base class for file-update transaction failures.

    ``retryable`` classifies the failure for the callers that can act on one —
    today the watch worker (Step 16). It answers a single question: would
    running the very same transaction again, unchanged, plausibly succeed?

    * A provider outage or a store that could not be reconciled: yes. The
      inputs are fine and the environment moved.
    * A file saved mid-edit with a syntax error, an unreadable file, a
      wrong-width embedding batch: no. Retrying re-reads the same broken
      state, and the fix — the developer saving again — arrives as a new
      filesystem event with its own fresh attempt budget.

    Terminal is not silent: an unretried failure is still reported through
    :attr:`BackgroundIndexer.failures`.
    """

    retryable = False


class FileUpdatePreparationError(FileUpdateError):
    """Raised when a replacement could not be built. Nothing was mutated.

    Carries every reason rather than the first, so one message names the whole
    problem instead of forcing a fix-and-retry loop.
    """

    def __init__(
        self, file_path: str, reasons: Sequence[str], *, retryable: bool = False
    ) -> None:
        self.file_path = file_path
        self.reasons = tuple(reasons)
        self.retryable = retryable
        super().__init__(
            f"Cannot prepare an index update for {file_path}: {'; '.join(reasons)}"
        )


class FileUpdateCommitError(FileUpdateError):
    """Raised when a commit left the chunk and vector stores disagreeing.

    ``recovered`` is always ``False`` here: a commit whose vector phase failed
    but was rebuilt from the durable chunk rows returns normally with
    ``FileUpdateCommit.recovered`` set instead. Seeing this exception means the
    generation cannot be trusted and must be discarded or rebuilt before
    readers use it.
    """

    #: The chunk rows committed; only the vector side is behind. Re-running the
    #: transaction re-derives it, so this is worth another attempt.
    retryable = True

    def __init__(self, file_path: str, reason: str) -> None:
        self.file_path = file_path
        self.reason = reason
        self.recovered = False
        super().__init__(
            f"Index update for {file_path} left the chunk and vector stores "
            f"inconsistent: {reason}"
        )


@dataclass(frozen=True)
class PreparedFileUpdate:
    """A complete, validated replacement for one file's chunk generation.

    Holding one of these implies parsing, chunking, and embedding all
    succeeded; nothing in the live index has been touched.
    """

    file_path: str
    chunks: tuple[CodeChunk, ...]
    dimension: int
    reused_embeddings: int = 0
    embedded_chunks: int = 0
    parse_errors: tuple[str, ...] = ()

    @property
    def is_deletion(self) -> bool:
        """Whether committing this update simply removes the file."""
        return not self.chunks

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(chunk.id for chunk in self.chunks)


@dataclass(frozen=True)
class FileUpdateCommit:
    """What one committed file update changed."""

    file_path: str
    previous_chunk_ids: tuple[str, ...] = ()
    committed_chunk_ids: tuple[str, ...] = ()
    removed_chunk_ids: tuple[str, ...] = ()
    generation_metadata: Mapping[str, Any] = field(default_factory=dict)
    recovered: bool = False

    @property
    def chunk_count(self) -> int:
        return len(self.committed_chunk_ids)

    @property
    def is_deletion(self) -> bool:
        return not self.committed_chunk_ids


@dataclass(frozen=True)
class FileMoveCommit:
    """A move: the new identity is committed first, then the old is removed.

    That order is deliberate. Preparing and committing the destination before
    dropping the source means an embedding failure leaves the *source*
    generation fully searchable, and the worst post-commit outcome is a
    duplicate that a retry clears — never a file that exists under neither
    identity.
    """

    committed: FileUpdateCommit
    removed: FileUpdateCommit


def validate_prepared_chunks(
    file_path: str, chunks: Sequence[CodeChunk], dimension: int
) -> None:
    """Reject a replacement that could not be committed consistently.

    Raises:
        FileUpdatePreparationError: A chunk has no embedding, the wrong
            dimension, a non-finite component, or a duplicate id.
    """
    reasons: list[str] = []
    seen: set[str] = set()

    for chunk in chunks:
        if chunk.id in seen:
            reasons.append(f"duplicate chunk id {chunk.id!r}")
        seen.add(chunk.id)

        embedding = chunk.embedding
        if embedding is None:
            reasons.append(f"chunk {chunk.id!r} has no embedding")
            continue
        if len(embedding) != dimension:
            reasons.append(
                f"chunk {chunk.id!r} has dimension {len(embedding)}, "
                f"expected {dimension}"
            )
            continue
        if any(not math.isfinite(float(value)) for value in embedding):
            reasons.append(f"chunk {chunk.id!r} has a non-finite embedding")

    if reasons:
        raise FileUpdatePreparationError(file_path, reasons)
