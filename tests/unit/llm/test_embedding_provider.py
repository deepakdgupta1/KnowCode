"""Unit tests for embedding providers."""

from __future__ import annotations

from typing import Any

import pytest

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import EmbeddingConfig
from knowcode.llm.embedding import (
    _VOYAGE_EMBED_DIMENSIONS,
    DummyEmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageAIEmbeddingProvider,
    build_provider_from_model,
    create_embedding_provider,
    create_prose_embedding_provider,
    resolve_embedding_dimension,
)


def test_embedding_provider_empty_batch() -> None:
    """Embedding provider should return empty list for empty input."""
    provider = OpenAIEmbeddingProvider(EmbeddingConfig())
    assert provider.embed([]) == []


def test_embedding_provider_normalize_zero_vector() -> None:
    """Normalization should handle zero vectors safely."""
    provider = OpenAIEmbeddingProvider(EmbeddingConfig())
    assert provider._normalize([0.0, 0.0]) == [0.0, 0.0]


# --- voyage-3-large registration -------------------------------------------


def test_voyage_3_large_is_registered_with_1024_dimensions() -> None:
    """voyage-3-large (prose tier) must be a known 1024-dim Voyage model."""
    assert _VOYAGE_EMBED_DIMENSIONS["voyage-3-large"] == 1024


def test_resolve_embedding_dimension_for_voyage_3_large() -> None:
    """Dimension resolution should report 1024 for voyage-3-large."""
    assert resolve_embedding_dimension("voyageai", "voyage-3-large") == 1024


def test_resolve_embedding_dimension_unknown_voyage_defaults_to_1024() -> None:
    """Unknown Voyage models fall back to the default 1024 dimension."""
    assert resolve_embedding_dimension("voyageai", "voyage-future-x") == 1024


def test_resolve_embedding_dimension_unknown_provider_raises() -> None:
    """Unrecognized providers must fail loudly instead of silently returning 1024.

    Regression guard: a duplicate lenient ``resolve_embedding_dimension`` once
    shadowed the fail-loud version and returned 1024 for any provider string,
    which silently produced a wrong-dimension index for misspelled providers.
    """
    with pytest.raises(ValueError, match="provider"):
        resolve_embedding_dimension("acme", "whatever")


def test_voyage_provider_uses_asymmetric_input_types() -> None:
    """Documents use input_type='document', queries use input_type='query'."""
    captured: list[dict[str, Any]] = []

    class _FakeClient:
        def embed(
            self, texts: list[str], model: str, input_type: str
        ) -> list[list[float]]:
            captured.append({"texts": texts, "model": model, "input_type": input_type})
            return [[0.1, 0.2, 0.3] for _ in texts]

    cfg = EmbeddingConfig(
        provider="voyageai", model_name="voyage-3-large", dimension=1024
    )
    provider = VoyageAIEmbeddingProvider(cfg)
    provider.client = _FakeClient()  # bypass network/credentials

    provider.embed(["doc one", "doc two"])
    provider.embed_single("a query")

    assert captured[0]["input_type"] == "document"
    assert captured[0]["model"] == "voyage-3-large"
    assert captured[1]["input_type"] == "query"
    assert captured[1]["model"] == "voyage-3-large"


# --- build_provider_from_model (provider dispatch seam) --------------------


def test_build_provider_from_model_voyageai(monkeypatch: pytest.MonkeyPatch) -> None:
    """A voyageai ModelConfig builds a VoyageAIEmbeddingProvider with right dim."""
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    model = ModelConfig(
        name="voyage-3-large",
        provider="voyageai",
        api_key_env="VOYAGE_API_KEY_1",
    )

    provider = build_provider_from_model(model)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.config.model_name == "voyage-3-large"
    assert provider.config.dimension == 1024
    assert provider.api_key_env == "VOYAGE_API_KEY_1"


def test_build_provider_from_model_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """An openai ModelConfig builds an OpenAIEmbeddingProvider."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ModelConfig(
        name="text-embedding-3-small",
        provider="openai",
        api_key_env="OPENAI_API_KEY",
    )

    provider = build_provider_from_model(model)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.config.model_name == "text-embedding-3-small"
    assert provider.config.dimension == 1536


def test_build_provider_from_model_local_not_yet_implemented() -> None:
    """The local provider seam must fail loudly (not silently) until implemented."""
    model = ModelConfig(
        name="Qwen3-Embedding",
        provider="local",
        api_key_env="UNUSED",
    )

    with pytest.raises(NotImplementedError, match="local"):
        build_provider_from_model(model)


def test_build_provider_from_model_unknown_provider_raises() -> None:
    """Unknown providers must raise rather than silently degrade."""
    model = ModelConfig(name="mystery", provider="acme", api_key_env="UNUSED")

    with pytest.raises(ValueError, match="provider"):
        build_provider_from_model(model)


# --- create_prose_embedding_provider (selector + fallback) -----------------


def test_create_prose_provider_uses_prose_model_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a usable prose model exists, prose selection uses it (not the code model)."""
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    app_config = AppConfig(
        embedding_models=[
            ModelConfig(
                name="voyage-code-3",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_1",
            )
        ],
        prose_embedding_models=[
            ModelConfig(
                name="voyage-3-large",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_1",
            )
        ],
    )

    provider = create_prose_embedding_provider(app_config=app_config)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.config.model_name == "voyage-3-large"


def test_create_prose_provider_falls_back_to_code_embedder_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no prose models configured, prose selection falls back to the code embedder."""
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    app_config = AppConfig(
        embedding_models=[
            ModelConfig(
                name="voyage-code-3",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_1",
            )
        ],
        prose_embedding_models=[],
    )

    prose_provider = create_prose_embedding_provider(app_config=app_config)
    code_provider = create_embedding_provider(app_config=app_config)

    assert isinstance(prose_provider, VoyageAIEmbeddingProvider)
    assert (
        prose_provider.config.model_name
        == code_provider.config.model_name
        == "voyage-code-3"
    )


def test_create_prose_provider_falls_back_when_prose_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the prose model's key is absent, fall back to a usable code embedder."""
    monkeypatch.delenv("VOYAGE_API_KEY_PROSE", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    app_config = AppConfig(
        embedding_models=[
            ModelConfig(
                name="voyage-code-3",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_1",
            )
        ],
        prose_embedding_models=[
            ModelConfig(
                name="voyage-3-large",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_PROSE",
            )
        ],
    )

    provider = create_prose_embedding_provider(app_config=app_config)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.config.model_name == "voyage-code-3"


def test_create_prose_provider_none_config_returns_dummy() -> None:
    """Without any AppConfig, prose selection degrades to the deterministic dummy."""
    provider = create_prose_embedding_provider(app_config=None)
    assert isinstance(provider, DummyEmbeddingProvider)


# --- concurrent client initialization ---------------------------------------
#
# Indexing embeds several batches at once, so two threads can reach a
# provider's lazy ``_get_client`` before either has finished building one.
# Unguarded, that builds two clients and throws one away — and for VoyageAI it
# runs the credential lookup twice.


def _build_clients_concurrently(
    provider: Any, monkeypatch: pytest.MonkeyPatch, factory_target: str
) -> int:
    """Call ``embed`` from several threads at once; count clients built."""
    import threading

    built = 0
    lock = threading.Lock()
    start = threading.Barrier(4)

    def slow_factory(*args: Any, **kwargs: Any) -> Any:
        nonlocal built
        with lock:
            built += 1
        # Widen the window a real factory (a network handshake) would open.
        import time

        time.sleep(0.05)
        return _StubClient()

    monkeypatch.setattr(factory_target, slow_factory)

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait(timeout=5)
            provider.embed(["text"])
        except BaseException as exc:  # noqa: BLE001 - re-raised by the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    return built


class _StubClient:
    """Answers both provider SDK shapes with fixed-width vectors."""

    def __init__(self) -> None:
        self.embeddings = self

    def create(self, model: str, input: list[str]) -> Any:
        class _Item:
            def __init__(self) -> None:
                self.embedding = [0.1, 0.2, 0.3]

        class _Response:
            def __init__(self, count: int) -> None:
                self.data = [_Item() for _ in range(count)]

        return _Response(len(input))

    def embed(self, texts: list[str], model: str, input_type: str) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_openai_builds_one_client_under_concurrent_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "key")
    provider = OpenAIEmbeddingProvider(
        EmbeddingConfig(), api_key_env="OPENAI_API_KEY_TEST"
    )
    provider.client = None

    built = _build_clients_concurrently(
        provider, monkeypatch, "knowcode.llm.embedding._create_openai_client"
    )

    assert built == 1


def test_voyageai_builds_one_client_under_concurrent_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VoyageAIEmbeddingProvider(EmbeddingConfig(provider="voyageai"))

    built = _build_clients_concurrently(
        provider, monkeypatch, "knowcode.llm.voyageai_client.get_voyageai_client"
    )

    assert built == 1


# --- The no-key fallback is labelled as what it is (BL-27) -----------------


def test_dummy_fallback_reports_dummy_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dummy-built generation must not claim to be a VoyageAI build."""
    monkeypatch.delenv("VOYAGE_API_KEY_1", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    provider = create_embedding_provider(app_config=AppConfig(embedding_models=[model]))

    assert isinstance(provider, DummyEmbeddingProvider)
    assert provider.config.provider == "dummy"
    assert provider.config.model_name == "deterministic-sha256"


def test_dummy_metadata_cannot_pass_for_real_voyage_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fields index compatibility compares must differ, dummy vs real."""
    monkeypatch.delenv("VOYAGE_API_KEY_1", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )
    dummy = create_embedding_provider(app_config=AppConfig(embedding_models=[model]))

    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    real = build_provider_from_model(model)

    mismatches = [
        key
        for key in ("provider", "model_name", "dimension", "normalize")
        if getattr(dummy.config, key) != getattr(real.config, key)
    ]
    assert mismatches, "dummy and real embedding metadata must not be interchangeable"


def test_prose_dummy_fallback_reports_dummy_metadata() -> None:
    """The prose fallback inherits the same honest labels."""
    provider = create_prose_embedding_provider(app_config=None)
    assert isinstance(provider, DummyEmbeddingProvider)
    assert provider.config.provider == "dummy"


# --- VoyageAI can route through the LiteLLM proxy (BL-26) ------------------


def test_voyage_provider_reads_proxy_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    monkeypatch.setenv("VOYAGE_BASE_URL", "http://127.0.0.1:4000")
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    provider = build_provider_from_model(model)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.base_url == "http://127.0.0.1:4000"


def test_voyage_proxy_embeddings_keep_input_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """input_type rides in the request body through the OpenAI-compatible path."""
    from types import SimpleNamespace

    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    captured: dict[str, Any] = {}

    class FakeEmbeddings:
        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0])])

    provider = VoyageAIEmbeddingProvider(
        EmbeddingConfig(), base_url="http://127.0.0.1:4000"
    )
    provider._proxy_client = SimpleNamespace(embeddings=FakeEmbeddings())

    provider.embed_single("query text")

    assert captured["model"] == "voyage/voyage-code-3"
    assert captured["extra_body"] == {"input_type": "query"}


def test_voyage_provider_without_proxy_env_keeps_the_native_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No VOYAGE_BASE_URL means the default direct VoyageAI route is kept."""
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    monkeypatch.delenv("VOYAGE_BASE_URL", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    provider = build_provider_from_model(model)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.base_url is None
