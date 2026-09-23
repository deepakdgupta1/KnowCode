"""Embedding providers for semantic search."""

from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import math
import os
import threading
from typing import Any, cast

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import EmbeddingConfig
from knowcode.llm.routing import DEFAULT_LITELLM_BASE_URL
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)

#: Labels the no-key fallback records, so a dummy-built generation is never
#: mistaken for a real one (BL-27) and never vouched for (BL-35).
DUMMY_EMBEDDING_PROVIDER = "dummy"
DUMMY_EMBEDDING_MODEL_NAME = "deterministic-sha256"

_OPENAI_EMBED_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

_VOYAGE_EMBED_DIMENSIONS: dict[str, int] = {
    "voyage-3-lite": 1024,
    "voyage-3": 1024,
    "voyage-3-large": 1024,
    "voyage-code-3": 1024,
}


def _create_openai_client(api_key: str, base_url: str | None) -> Any:
    """Create an OpenAI client with an actionable dependency hint."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on environment extras
        raise ImportError("Install knowcode[llm] to use 'knowcode index'.") from exc
    return OpenAI(api_key=api_key, base_url=base_url)


class EmbeddingProvider(ABC):
    """Abstract interface for generating embeddings."""

    def __init__(self, config: EmbeddingConfig) -> None:
        """Initialize the provider with the embedding configuration."""
        self.config = config

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        pass

    @abstractmethod
    def embed_single(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        pass


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embedding provider."""

    def __init__(
        self,
        config: EmbeddingConfig,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
    ) -> None:
        """Create an OpenAI-backed embedding provider.

        Args:
            config: Embedding configuration settings.
            api_key_env: Environment variable containing the API key.
            base_url: Optional base URL for OpenAI-compatible providers.
        """
        super().__init__(config)
        self.api_key_env = api_key_env
        self.base_url = base_url
        # Indexing embeds several batches at once, so client construction is a
        # contended path: guard it rather than build one client per thread.
        self._client_lock = threading.Lock()

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            # We allow init without key, but embed() will fail if not provided later
            self.client = None
        else:
            self.client = _create_openai_client(api_key=api_key, base_url=base_url)

    def _get_client(self) -> Any:
        """Return an initialized OpenAI client, loading credentials if needed.

        Double-checked because concurrent batches all reach this before any of
        them has a client: unguarded, each thread builds its own and discards
        all but the last.
        """
        client = self.client
        if client is not None:
            return client

        with self._client_lock:
            if self.client is None:
                api_key = os.environ.get(self.api_key_env)
                if not api_key:
                    raise ValueError(
                        f"{self.api_key_env} environment variable is not set."
                    )
                self.client = _create_openai_client(
                    api_key=api_key, base_url=self.base_url
                )
            return self.client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: Input texts to embed.

        Returns:
            List of embedding vectors (one per input).
        """
        if not texts:
            return []

        client = self._get_client()
        response = client.embeddings.create(model=self.config.model_name, input=texts)
        embeddings = [item.embedding for item in response.data]

        if self.config.normalize:
            embeddings = [self._normalize(e) for e in embeddings]

        return embeddings

    def embed_single(self, text: str) -> list[float]:
        """Generate an embedding for a single text input."""
        return self.embed([text])[0]

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        import math

        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec


class VoyageAIEmbeddingProvider(EmbeddingProvider):
    """VoyageAI embedding provider."""

    def __init__(
        self,
        config: EmbeddingConfig,
        api_key_env: str = "VOYAGE_API_KEY_1",
        base_url: str | None = None,
    ) -> None:
        """Create a VoyageAI-backed embedding provider.

        Args:
            config: Embedding configuration settings.
            api_key_env: Environment variable containing the VoyageAI API key.
            base_url: When set, send embeddings through the OpenAI-compatible
                endpoint at this address (the LiteLLM proxy) instead of the
                native VoyageAI SDK client. The proxy is the route model
                traffic is required to take where one is deployed; without it
                the SDK client remains the default so a direct VoyageAI key
                keeps working unchanged.
        """
        super().__init__(config)
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.client: Any = None
        self._proxy_client: Any = None
        # See OpenAIEmbeddingProvider: concurrent batches race this, and here
        # the loser also repeats the credential lookup.
        self._client_lock = threading.Lock()

    def _get_client(self) -> Any:
        """Return an initialized VoyageAI client, loading credentials if needed."""
        client = self.client
        if client is not None:
            return client

        with self._client_lock:
            if self.client is None:
                from knowcode.llm.voyageai_client import get_voyageai_client

                self.client = get_voyageai_client(self.api_key_env)

            if self.client is None:
                raise ValueError(
                    f"VoyageAI client unavailable; set {self.api_key_env} and install "
                    'optional dependency with: pip install "knowcode[voyageai]"'
                )

            return self.client

    def _get_proxy_client(self) -> Any:
        """Return an OpenAI-compatible client aimed at ``base_url``."""
        client = self._proxy_client
        if client is not None:
            return client

        with self._client_lock:
            if self._proxy_client is None:
                api_key = os.environ.get(self.api_key_env)
                if not api_key:
                    raise ValueError(
                        f"{self.api_key_env} environment variable is not set."
                    )
                self._proxy_client = _create_openai_client(
                    api_key=api_key, base_url=self.base_url
                )
            return self._proxy_client

    def _embed_via_proxy(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Embed through the OpenAI-compatible proxy endpoint.

        The model name carries the ``voyage/`` prefix LiteLLM routes on, and
        ``input_type`` travels in the request body -- the one Voyage-specific
        field the OpenAI schema has no slot for -- so proxied embeddings keep
        the query/document distinction the native client sends.
        """
        client = self._get_proxy_client()
        response = client.embeddings.create(
            model=f"voyage/{self.config.model_name}",
            input=texts,
            extra_body={"input_type": input_type},
        )
        embeddings = [item.embedding for item in response.data]
        if not embeddings:
            return []
        if self.config.normalize:
            embeddings = [self._normalize(e) for e in embeddings]
        return cast(list[list[float]], embeddings)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate document embeddings for a batch of texts."""
        if not texts:
            return []

        if self.base_url:
            return self._embed_via_proxy(texts, "document")

        client = self._get_client()
        embeddings = client.embed(
            texts=texts,
            model=self.config.model_name,
            input_type="document",
        )
        if not embeddings:
            return []

        if self.config.normalize:
            embeddings = [self._normalize(e) for e in embeddings]

        return cast(list[list[float]], embeddings)

    def embed_single(self, text: str) -> list[float]:
        """Generate a query embedding for a single text input."""
        if self.base_url:
            embeddings = self._embed_via_proxy([text], "query")
            if not embeddings:
                return []
            emb = embeddings[0]
            return self._normalize(emb) if self.config.normalize else emb

        client = self._get_client()
        embeddings = client.embed(
            texts=[text],
            model=self.config.model_name,
            input_type="query",
        )
        if not embeddings:
            return []

        emb = embeddings[0]
        return self._normalize(emb) if self.config.normalize else emb

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        import math

        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec


class DummyEmbeddingProvider(EmbeddingProvider):
    """Deterministic embedding fallback that requires no external API keys."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate stable pseudo-embeddings for a batch of texts."""
        return [self.embed_single(text) for text in texts]

    def embed_single(self, text: str) -> list[float]:
        """Generate a stable pseudo-embedding for one text."""
        dimension = max(0, self.config.dimension)
        if dimension == 0:
            return []

        values: list[float] = []
        seed = text.encode("utf-8", errors="replace")
        counter = 0
        while len(values) < dimension:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            values.extend((byte / 127.5) - 1.0 for byte in digest)
            counter += 1

        vector = values[:dimension]
        return self._normalize(vector) if self.config.normalize else vector

    def _normalize(self, vec: list[float]) -> list[float]:
        """Normalize a vector to unit length for cosine similarity."""
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec


#: Accepted spellings, aliases included, of each embedding provider family.
_VOYAGE_PROVIDERS = frozenset({"voyageai", "voyage"})
_OPENAI_PROVIDERS = frozenset({"openai", "openrouter", "mistralai"})

#: The spellings worth suggesting to someone whose `provider` is unsupported.
#: Aliases are accepted above but not advertised here, so the hint reads as a
#: list of distinct choices rather than of synonyms.
SUPPORTED_EMBEDDING_PROVIDERS: tuple[str, ...] = (
    "mistralai",
    "openai",
    "openrouter",
    "voyageai",
)


def resolve_embedding_dimension(provider: str, model_name: str) -> int:
    """Resolve the vector dimension for a configured embedding model."""
    normalized_provider = provider.lower()
    if normalized_provider in _VOYAGE_PROVIDERS:
        return _VOYAGE_EMBED_DIMENSIONS.get(model_name, 1024)
    if normalized_provider in _OPENAI_PROVIDERS:
        return _OPENAI_EMBED_DIMENSIONS.get(model_name, 1536)
    raise ValueError(f"Unsupported embedding provider: {provider}")


def embedding_config_for_model(model: ModelConfig) -> EmbeddingConfig:
    """Describe one configured embedding model, without building a client.

    The config half of :func:`build_provider_from_model`, split out so the
    labels a build records and the labels a diagnostic compares against come
    from one derivation. Reachable without the model's API key, which is what
    lets :func:`configured_embedding_config` answer for an unusable model.
    """
    provider = model.provider.lower()

    if provider in _VOYAGE_PROVIDERS:
        return EmbeddingConfig(
            provider="voyageai",
            model_name=model.name,
            dimension=resolve_embedding_dimension(provider, model.name),
        )

    if provider in _OPENAI_PROVIDERS:
        return EmbeddingConfig(
            provider="openai",
            model_name=model.name,
            dimension=resolve_embedding_dimension(provider, model.name),
        )

    if provider == "local":
        raise NotImplementedError("The local embedding provider is not implemented.")

    raise ValueError(f"Unsupported embedding provider: {model.provider}")


def build_provider_from_model(model: ModelConfig) -> EmbeddingProvider:
    """Build an embedding provider from one model configuration."""
    config = embedding_config_for_model(model)

    if config.provider == "voyageai":
        return VoyageAIEmbeddingProvider(
            config,
            api_key_env=model.api_key_env,
            base_url=os.environ.get("VOYAGE_BASE_URL") or DEFAULT_LITELLM_BASE_URL,
        )

    base_url = (
        "https://openrouter.ai/api/v1"
        if model.provider.lower() in {"openrouter", "mistralai"}
        else None
    )
    return OpenAIEmbeddingProvider(
        config,
        api_key_env=model.api_key_env,
        base_url=base_url,
    )


def dummy_embedding_config() -> EmbeddingConfig:
    """Return the labels a dummy-built generation records."""
    return EmbeddingConfig(
        provider=DUMMY_EMBEDDING_PROVIDER,
        model_name=DUMMY_EMBEDDING_MODEL_NAME,
        dimension=1024,
    )


def _dummy_embedding_provider() -> EmbeddingProvider:
    """Build the no-key fallback, labelled as what it is.

    The dummy used to inherit ``EmbeddingConfig``'s voyageai defaults, so a
    generation built without a key persisted ``provider=voyageai,
    model_name=voyage-code-3`` -- metadata indistinguishable from a real
    VoyageAI build. Index compatibility then accepted the dummy-built index
    once a key appeared, and dense retrieval compared real query vectors
    against sha256 pseudo-vectors with no warning. The honest labels make
    that mismatch visible exactly where it is checked. The dimension stays
    1024 so a dummy-built index remains self-consistent for offline use.
    """
    return DummyEmbeddingProvider(dummy_embedding_config())


def configured_embedding_config(app_config: AppConfig | None) -> EmbeddingConfig:
    """Return the embedding the configuration asks for, whatever the environment.

    The key-independent mirror of :func:`effective_embedding_config`. A
    diagnostic needs both. The effective config says what this process would
    embed with; the configured config says what was asked for. BL-35 is what
    happens when only the first exists: with no key set, a dummy-built index
    and a dummy-resolving runtime agree, and the comparison proves nothing.

    A model this build cannot describe is reported and stepped over rather
    than raised, because a diagnostic that crashes on a bad config line
    diagnoses nothing at all. With every entry unusable the dummy is what the
    configuration effectively asks for, which is what this returns.
    """
    for model in (app_config.embedding_models if app_config else None) or ():
        try:
            return embedding_config_for_model(model)
        except (NotImplementedError, ValueError) as exc:
            logger.warning(
                "Configured embedding model %r (provider %r) cannot be used: %s",
                model.name,
                model.provider,
                exc,
            )
    return dummy_embedding_config()


def effective_embedding_config(app_config: AppConfig | None) -> EmbeddingConfig:
    """Return the configuration ``create_embedding_provider`` would use.

    Key-aware, like the factory it mirrors: with no usable embedding model
    in the environment the expected configuration is the honestly-labelled
    dummy, because that is what the runtime would actually query with. An
    expectation copied from the config but ignoring the environment
    diagnosed indexes the running stack could never produce or consume.
    """
    return create_embedding_provider(app_config=app_config).config


def create_embedding_provider(
    app_config: AppConfig | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> EmbeddingProvider:
    """Create an embedding provider from explicit config or AppConfig.

    Selection precedence:
    1) Explicit EmbeddingConfig (embedding_config.provider)
    2) First usable model in app_config.embedding_models
    3) Default OpenAIEmbeddingProvider with default EmbeddingConfig
    """
    if embedding_config is not None:
        provider = embedding_config.provider.lower()
        if provider in {"voyageai", "voyage"}:
            return VoyageAIEmbeddingProvider(embedding_config)
        return OpenAIEmbeddingProvider(embedding_config)

    if app_config and app_config.embedding_models:
        unusable: list[str] = []
        for model in app_config.embedding_models:
            if not os.environ.get(model.api_key_env):
                unusable.append(f"{model.name} (no {model.api_key_env} is set)")
                continue

            # The predicate is `embedding_config_for_model` itself, not a catch
            # around the build, so "skipped here" and "named by doctor's Config
            # check" are the same question asked of the same function. Catching
            # around the build would also swallow a genuine construction error
            # as though it were an unsupported provider.
            try:
                embedding_config_for_model(model)
            except (NotImplementedError, ValueError) as exc:
                unusable.append(_describe_unusable_model(model, exc))
                continue

            return build_provider_from_model(model)

        _warn_dummy_fallback(unusable)

    return _dummy_embedding_provider()


def _describe_unusable_model(model: ModelConfig, exc: Exception) -> str:
    """Name a configured entry and why this build cannot construct it."""
    return f"{model.name} (provider {model.provider!r}: {exc})"


def _warn_dummy_fallback(reasons: list[str]) -> None:
    """Say that a configured embedder was asked for and not delivered.

    BL-35: a build with no key in the environment printed a chunk count and an
    index path, both true of a file that exists, and named the embedder
    nowhere. Everything downstream then reads as a working semantic plane. The
    one place that knows the substitution happened is the selection itself, so
    it is the one place that can say so for every caller.

    BL-36 gave the substitution a second cause. Selection now steps over an
    entry whose provider this build cannot construct rather than raising, so
    the fallback is reachable with every key in the environment, and the
    sentence names the reason per entry instead of asserting a missing key.
    """
    logger.warning(
        "No configured embedding model is usable, so embeddings fall back to "
        "%s, whose vectors are deterministic hashes carrying no semantic "
        "signal. Semantic ranking will not work until one of these is "
        "resolved: %s.",
        DUMMY_EMBEDDING_MODEL_NAME,
        "; ".join(reasons),
    )


def create_prose_embedding_provider(
    app_config: AppConfig | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> EmbeddingProvider:
    """Create an embedding provider for SDLC prose documentation collateral.

    Selection precedence:
    1) Explicit EmbeddingConfig (embedding_config.provider).
    2) The first usable model in app_config.prose_embedding_models (one whose
       API key is present in the environment).
    3) Fall back to the code embedding provider (create_embedding_provider),
       so prose retrieval degrades to the code embedder when no dedicated prose
       model is usable.
    4) DummyEmbeddingProvider when no AppConfig is given at all.

    Args:
        app_config: Optional application configuration with prose/code model lists.
        embedding_config: Optional explicit embedding configuration.

    Returns:
        A concrete EmbeddingProvider for prose chunks.
    """
    if embedding_config is not None:
        provider = embedding_config.provider.lower()
        if provider in {"voyageai", "voyage"}:
            return VoyageAIEmbeddingProvider(embedding_config)
        return OpenAIEmbeddingProvider(embedding_config)

    if app_config and app_config.prose_embedding_models:
        for model in app_config.prose_embedding_models:
            if not os.environ.get(model.api_key_env):
                continue
            # BL-36, the same gate on the prose plane: an unbuildable provider
            # would otherwise raise out of a build rather than fall through to
            # the code embedder this function already promises as its step 3.
            try:
                embedding_config_for_model(model)
            except (NotImplementedError, ValueError) as exc:
                logger.warning(
                    "Skipping prose embedding model %s.",
                    _describe_unusable_model(model, exc),
                )
                continue

            return build_provider_from_model(model)

    # No usable prose model — fall back to the code embedder, then the dummy.
    if app_config and (app_config.embedding_models or app_config.models):
        code_provider = create_embedding_provider(app_config=app_config)
        # create_embedding_provider already returns the honestly-labelled
        # dummy when nothing is usable, so no further fallback is required.
        return code_provider

    return _dummy_embedding_provider()
