"""Batched, bounded-concurrency, retrying embedding for the index pipelines.

Embedding is the only network hop in a build, and it used to be issued one
file at a time, one call after another, with no second attempt::

    500 files -> 500 sequential round-trips; one blip fails a file outright

This module owns the three fixes together, because they only work as a set:

* **Batching** cuts at the provider's own limit rather than at a file
  boundary, so a repository of small files stops paying one round-trip each.
* **Bounded concurrency** overlaps those batches. The bound is not decoration:
  every provider rate-limits, and an unbounded fan-out converts a fast build
  into a throttled one.
* **Retry with backoff** absorbs the transient failures that batching makes
  more expensive to hit — a batch now carries many files' chunks, so failing
  it on the first 503 would waste far more work than failing one file did.

A short batch is deliberately *not* retried. A provider that answers four
vectors for five texts will answer four again; that is a contract violation,
not weather. Everything else — timeouts, resets, rate limits, 5xx — is treated
as transient, because the alternative is a fragile allowlist of exception types
that misclassifies the one outage nobody predicted.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from knowcode.protocols import EmbeddingProviderProtocol
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Texts per provider call when the provider's config does not say. Both
#: supported providers accept more (OpenAI 2048, Voyage 128), so this is a
#: conservative floor rather than a limit worth tuning up blindly.
DEFAULT_BATCH_SIZE = 100

#: Batches in flight at once. Chosen low on purpose: embedding endpoints
#: rate-limit per key, and a 429 storm is slower than a modest steady rate.
DEFAULT_MAX_WORKERS = 4

#: Backoff before each re-attempt; ``delays[i]`` precedes attempt ``i + 2``.
#: Longer than the watch worker's because a rate-limited embedding endpoint
#: wants to be left alone, and a build is not racing a developer's next save.
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)


class EmbeddingBatchError(RuntimeError):
    """One batch of texts could not be embedded.

    ``retryable`` answers the same question :class:`~knowcode.indexing.
    file_updates.FileUpdateError` does: would running this very call again,
    unchanged, plausibly succeed? A provider outage, yes. A wrong-count
    response, no.
    """

    def __init__(self, reason: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(reason)


@dataclass(frozen=True)
class RetryPolicy:
    """How often one batch is re-attempted, and how long it waits between.

    ``RetryPolicy(delays=())`` means a single attempt, which is what the
    per-file fallback after a failed window uses: the window already spent a
    full budget, so re-spending one per file would multiply an outage by the
    window size.
    """

    delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS

    @property
    def attempts(self) -> int:
        """Total provider calls one batch may cost, including the first."""
        return len(self.delays) + 1


def _provider_batch_size(provider: EmbeddingProviderProtocol) -> int:
    """Read the provider's configured batch limit, defaulting when absent.

    Read defensively rather than off the dataclass field: the protocol only
    promises ``config.dimension``, and several callers pass a minimal config
    object that carries nothing else.
    """
    configured = getattr(getattr(provider, "config", None), "batch_size", None)
    if isinstance(configured, int) and not isinstance(configured, bool):
        if configured > 0:
            return configured
    return DEFAULT_BATCH_SIZE


class BatchEmbedder:
    """Embeds a flat list of texts as concurrent, retried, provider-sized batches.

    Order is part of the contract: the returned vectors line up with the input
    texts index for index, whatever order the batches actually completed in.
    Callers zip them straight onto chunks, so a reordering here would attach
    every vector to the wrong chunk without failing anything.
    """

    def __init__(
        self,
        provider: EmbeddingProviderProtocol,
        *,
        batch_size: Optional[int] = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        retry: Optional[RetryPolicy] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure batching, concurrency, and the retry budget.

        Args:
            provider: The embedding provider to call.
            batch_size: Texts per call. Defaults to the provider's configured
                limit, then to :data:`DEFAULT_BATCH_SIZE`.
            max_workers: Batches in flight at once. One runs every batch on the
                calling thread, which is what the single-file watch path wants.
            retry: Re-attempt budget for one batch.
            sleep: Injected so tests can assert the backoff without waiting it.
        """
        self._provider = provider
        resolved_batch_size = (
            batch_size if batch_size is not None else _provider_batch_size(provider)
        )
        self._batch_size = max(1, resolved_batch_size)
        self._max_workers = max(1, max_workers)
        self._retry = retry if retry is not None else RetryPolicy()
        self._sleep = sleep

    @property
    def batch_size(self) -> int:
        """Texts sent per provider call."""
        return self._batch_size

    @property
    def max_workers(self) -> int:
        """Batches allowed in flight at once."""
        return self._max_workers

    def embed(self, texts: Sequence[str], *, retry: bool = True) -> list[list[float]]:
        """Embed every text, returning vectors in the input's order.

        Args:
            texts: Texts to embed. An empty sequence never calls the provider.
            retry: Spend the configured retry budget. ``False`` makes exactly
                one attempt per batch, for a caller that has already spent one.

        Raises:
            EmbeddingBatchError: A batch failed, or a batch came back the wrong
                length. Nothing partial is returned: the caller decides whether
                to fall back to smaller units or give up.
        """
        if not texts:
            return []

        batches = [
            list(texts[start : start + self._batch_size])
            for start in range(0, len(texts), self._batch_size)
        ]
        delays = self._retry.delays if retry else ()

        if len(batches) == 1 or self._max_workers == 1:
            # No pool for the common small case: the watch path embeds a
            # handful of chunks and should not pay for a thread to do it.
            results = [self._embed_batch(batch, delays) for batch in batches]
        else:
            workers = min(self._max_workers, len(batches))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="knowcode-embed"
            ) as pool:
                futures = [
                    pool.submit(self._embed_batch, batch, delays) for batch in batches
                ]
                # Reading futures in submission order is what keeps the result
                # aligned with the input; completion order is not the contract.
                results = [future.result() for future in futures]

        vectors = [vector for result in results for vector in result]
        if len(vectors) != len(texts):
            # Unreachable while every batch validates its own length; kept so a
            # future batching bug cannot silently misalign chunks and vectors.
            raise EmbeddingBatchError(
                f"embedding provider returned {len(vectors)} vectors "
                f"for {len(texts)} chunks",
                retryable=False,
            )
        return vectors

    def _embed_batch(
        self, batch: list[str], delays: tuple[float, ...]
    ) -> list[list[float]]:
        """Embed one batch, re-attempting transient failures with backoff."""
        attempts = len(delays) + 1

        for attempt in range(1, attempts + 1):
            try:
                vectors = self._provider.embed(batch)
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                if attempt >= attempts:
                    raise EmbeddingBatchError(
                        f"embedding {len(batch)} texts failed after "
                        f"{attempts} attempt(s): {exc}",
                        retryable=True,
                    ) from exc
                delay = delays[attempt - 1]
                logger.warning(
                    "Embedding batch of %d failed (attempt %d/%d): %s; "
                    "retrying in %.1fs",
                    len(batch),
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
                continue

            if len(vectors) != len(batch):
                # Deterministic: the same request returns the same short
                # answer, so a retry only delays the report.
                raise EmbeddingBatchError(
                    f"embedding provider returned {len(vectors)} vectors "
                    f"for {len(batch)} chunks",
                    retryable=False,
                )
            return [list(vector) for vector in vectors]

        raise EmbeddingBatchError(  # pragma: no cover - loop always exits above
            "no embedding attempt was made", retryable=False
        )
