"""Batching, bounded concurrency, and retry for the embedding round-trip.

The reviewed defect: a build issued exactly one ``embed`` call per file and ran
them one after another, so an N-file repository cost N sequential network
round-trips and a single transient provider blip failed the file outright::

    500 files -> 500 sequential calls, no batching, no concurrency, no retry

This module owns the three fixes together, because they interact: batches are
cut at the provider's own limit, a bounded pool overlaps them, and each batch
carries its own retry budget so one blip does not surface as a build failure.

A short batch is deliberately *not* retried. A provider that answers 4 vectors
for 5 texts will answer 4 again; that is a contract violation, not weather.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import pytest

from knowcode.indexing.embedding_batch import (
    BatchEmbedder,
    EmbeddingBatchError,
    RetryPolicy,
)

DIMENSION = 3
NO_RETRY = RetryPolicy(delays=())


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


@dataclass
class _Config:
    dimension: int = DIMENSION
    batch_size: int = 100


class RecordingProvider:
    """Records every batch it was asked for, in call order."""

    def __init__(self, batch_size: int = 100) -> None:
        self.config = _Config(batch_size=batch_size)
        self.batches: list[list[str]] = []
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.batches.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float(len(text)), float(ord(text[0])) if text else 0.0, 1.0]


class FlakyProvider(RecordingProvider):
    """Fails a chosen number of times before answering normally."""

    def __init__(self, failures: int, batch_size: int = 100) -> None:
        super().__init__(batch_size=batch_size)
        self.remaining_failures = failures
        self.attempts = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.attempts += 1
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                raise RuntimeError("embedding provider is down")
        return super().embed(texts)


class ShortBatchProvider(RecordingProvider):
    """Answers one vector fewer than it was asked for, every time."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return super().embed(texts)[:-1]


class ConcurrencyProbe(RecordingProvider):
    """Blocks until a set number of calls overlap, recording the peak."""

    def __init__(self, release_at: int, batch_size: int = 100) -> None:
        super().__init__(batch_size=batch_size)
        self.release_at = release_at
        self.in_flight = 0
        self.peak_in_flight = 0
        self.calling_threads: set[str] = set()
        self._all_in = threading.Event()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.calling_threads.add(threading.current_thread().name)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            if self.in_flight >= self.release_at:
                self._all_in.set()
        # Every worker waits for the pool to fill, so a sequential runner
        # deadlocks here rather than passing by accident.
        self._all_in.wait(timeout=5.0)
        with self._lock:
            self.in_flight -= 1
        return super().embed(texts)


class RecordingSleep:
    """A sleep that records what it was asked to wait for, without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _texts(count: int, prefix: str = "t") -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


# ----------------------------------------------------------------------
# Batching
# ----------------------------------------------------------------------


def test_an_empty_request_never_touches_the_provider() -> None:
    provider = RecordingProvider()

    assert BatchEmbedder(provider).embed([]) == []
    assert provider.batches == []


def test_texts_are_split_at_the_providers_batch_limit() -> None:
    """The provider's own limit is the cut, not one call per caller."""
    provider = RecordingProvider(batch_size=4)
    embedder = BatchEmbedder(provider, max_workers=1)

    embedder.embed(_texts(10))

    assert [len(batch) for batch in provider.batches] == [4, 4, 2]


def test_an_explicit_batch_size_overrides_the_provider_config() -> None:
    provider = RecordingProvider(batch_size=100)
    embedder = BatchEmbedder(provider, batch_size=3, max_workers=1)

    embedder.embed(_texts(7))

    assert [len(batch) for batch in provider.batches] == [3, 3, 1]


def test_a_provider_without_a_batch_size_still_batches() -> None:
    """A minimal provider double exposes only ``dimension``; it must work."""

    class MinimalConfig:
        dimension = DIMENSION

    provider = RecordingProvider()
    provider.config = MinimalConfig()  # type: ignore[assignment]

    embedder = BatchEmbedder(provider, max_workers=1)
    result = embedder.embed(_texts(5))

    assert len(result) == 5
    assert len(provider.batches) == 1


def test_a_batch_size_below_one_is_clamped() -> None:
    provider = RecordingProvider()
    embedder = BatchEmbedder(provider, batch_size=0, max_workers=1)

    embedder.embed(_texts(3))

    assert [len(batch) for batch in provider.batches] == [1, 1, 1]


def test_results_keep_the_input_order_across_concurrent_batches() -> None:
    """Concurrency must not reorder vectors relative to their texts."""
    provider = RecordingProvider(batch_size=2)
    embedder = BatchEmbedder(provider, max_workers=4)

    texts = _texts(11, prefix="order")
    result = embedder.embed(texts)

    assert result == [RecordingProvider._vector(text) for text in texts]


# ----------------------------------------------------------------------
# Concurrency
# ----------------------------------------------------------------------


def test_batches_run_concurrently() -> None:
    """Four batches must overlap; a sequential runner cannot pass this."""
    provider = ConcurrencyProbe(release_at=4, batch_size=1)
    embedder = BatchEmbedder(provider, max_workers=4)

    result = embedder.embed(_texts(4))

    assert len(result) == 4
    assert provider.peak_in_flight == 4


def test_concurrency_never_exceeds_max_workers() -> None:
    """The bound is a real cap: providers have rate limits behind it."""
    provider = ConcurrencyProbe(release_at=2, batch_size=1)
    embedder = BatchEmbedder(provider, max_workers=2)

    embedder.embed(_texts(8))

    assert provider.peak_in_flight == 2


def test_a_single_worker_calls_the_provider_on_the_calling_thread() -> None:
    """max_workers=1 skips the pool: the watch path embeds one small batch."""
    provider = ConcurrencyProbe(release_at=1, batch_size=2)
    embedder = BatchEmbedder(provider, max_workers=1)

    embedder.embed(_texts(4))

    assert provider.calling_threads == {threading.current_thread().name}


# ----------------------------------------------------------------------
# Retry
# ----------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_then_succeeds() -> None:
    provider = FlakyProvider(failures=2)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.5, 2.0)), sleep=sleep
    )

    result = embedder.embed(_texts(3))

    assert len(result) == 3
    assert provider.attempts == 3


def test_retries_wait_for_the_configured_backoff() -> None:
    provider = FlakyProvider(failures=2)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.5, 2.0)), sleep=sleep
    )

    embedder.embed(_texts(1))

    assert sleep.delays == [0.5, 2.0]


def test_an_exhausted_retry_budget_reports_the_underlying_error() -> None:
    provider = FlakyProvider(failures=99)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.1,)), sleep=sleep
    )

    with pytest.raises(EmbeddingBatchError) as excinfo:
        embedder.embed(_texts(2))

    assert provider.attempts == 2
    assert excinfo.value.retryable is True
    # The watch worker matches on the provider's own words; keep them.
    assert "embedding provider is down" in str(excinfo.value)


def test_a_short_batch_is_not_retried() -> None:
    """A wrong-count answer is deterministic, so waiting cannot help."""
    provider = ShortBatchProvider(batch_size=100)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.5, 2.0)), sleep=sleep
    )

    with pytest.raises(EmbeddingBatchError) as excinfo:
        embedder.embed(_texts(5))

    assert len(provider.batches) == 1
    assert sleep.delays == []
    assert excinfo.value.retryable is False
    assert "4" in str(excinfo.value) and "5" in str(excinfo.value)


def test_retry_can_be_disabled_for_one_call() -> None:
    """The per-file fallback after a failed window must not re-spend a budget."""
    provider = FlakyProvider(failures=99)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.5, 2.0)), sleep=sleep
    )

    with pytest.raises(EmbeddingBatchError):
        embedder.embed(_texts(1), retry=False)

    assert provider.attempts == 1
    assert sleep.delays == []


def test_a_policy_with_no_delays_makes_exactly_one_attempt() -> None:
    provider = FlakyProvider(failures=99)
    embedder = BatchEmbedder(provider, max_workers=1, retry=NO_RETRY)

    with pytest.raises(EmbeddingBatchError):
        embedder.embed(_texts(1))

    assert provider.attempts == 1


def test_one_failing_batch_fails_the_whole_call() -> None:
    """The caller falls back per file, so a partial answer must not be returned."""

    class PoisonProvider(RecordingProvider):
        def embed(self, texts: list[str]) -> list[list[float]]:
            if any(text == "poison" for text in texts):
                raise RuntimeError("bad input")
            return super().embed(texts)

    provider = PoisonProvider(batch_size=1)
    embedder = BatchEmbedder(provider, max_workers=2, retry=NO_RETRY)

    with pytest.raises(EmbeddingBatchError):
        embedder.embed(["a", "poison", "b"])


def test_the_retry_budget_is_per_batch() -> None:
    """Each batch gets its own attempts; one blip must not spend another's."""
    provider = FlakyProvider(failures=1, batch_size=1)
    sleep = RecordingSleep()
    embedder = BatchEmbedder(
        provider, max_workers=1, retry=RetryPolicy(delays=(0.1,)), sleep=sleep
    )

    result = embedder.embed(_texts(3))

    assert len(result) == 3
    assert provider.attempts == 4


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_max_workers_below_one_is_clamped() -> None:
    provider = RecordingProvider()
    embedder = BatchEmbedder(provider, max_workers=0)

    assert embedder.max_workers == 1
    assert len(embedder.embed(_texts(2))) == 2


@pytest.mark.parametrize("configured,expected", [(None, 100), (7, 7)])
def test_the_effective_batch_size_is_visible(
    configured: Optional[int], expected: int
) -> None:
    provider = RecordingProvider(batch_size=100)

    assert BatchEmbedder(provider, batch_size=configured).batch_size == expected
