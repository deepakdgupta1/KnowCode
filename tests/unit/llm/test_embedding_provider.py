"""Unit tests for embedding providers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from knowcode.config import AppConfig, ModelConfig
from knowcode.data_models import EmbeddingConfig
from knowcode.llm.embedding import (
    _VOYAGE_EMBED_DIMENSIONS,
    SUPPORTED_EMBEDDING_PROVIDERS,
    DummyEmbeddingProvider,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageAIEmbeddingProvider,
    build_provider_from_model,
    configured_embedding_config,
    create_embedding_provider,
    create_prose_embedding_provider,
    effective_embedding_config,
    embedding_config_for_model,
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

    assert captured["model"] == "voyage-code-3"
    assert captured["extra_body"] == {"input_type": "query"}


def test_voyage_provider_targets_litellm_proxy_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No VOYAGE_BASE_URL still keeps outbound model traffic on LiteLLM."""
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    monkeypatch.delenv("VOYAGE_BASE_URL", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    provider = build_provider_from_model(model)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.base_url == "http://127.0.0.1:4000"


# --- A proxied embedding request is one the proxy accepts (BL-42) -----------


@pytest.fixture
def proxy_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Stand in for the proxy on a local socket and record every request."""
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {"authorization": self.headers["Authorization"], "body": body}
            )
            data = [
                {"object": "embedding", "index": index, "embedding": [1.0, 0.0]}
                for index in range(len(body["input"]))
            ]
            reply = json.dumps(
                {"object": "list", "model": body["model"], "data": data, "usage": {}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VOYAGE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("section", "select"),
    [
        ("embedding_models", create_embedding_provider),
        ("prose_embedding_models", create_prose_embedding_provider),
        (None, create_embedding_provider),
    ],
    ids=["code-entry", "prose-entry", "no-config-file"],
)
def test_an_entry_that_names_no_key_sends_a_request_the_proxy_accepts(
    section: str | None,
    select: Callable[..., EmbeddingProvider],
    proxy_requests: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy authenticates with its own key and serves models by its own names.

    Both halves failed against the real proxy while stubbed clients passed.
    Sent the provider's key, LiteLLM reads it as a virtual key and, with no
    database, answers ``400 No connected db``; asked for
    ``voyage/voyage-code-3`` it answers ``Invalid model name``. So this reads
    the request off the wire rather than off the client object.
    """
    monkeypatch.setenv("LITELLM_MASTER_KEY", "proxy-key")
    monkeypatch.setenv("VOYAGE_API_KEY_1", "provider-key")
    if section is None:
        app_config = AppConfig.default()
    else:
        path = tmp_path / "aimodels.yaml"
        path.write_text(
            f"{section}:\n  - name: voyage-code-3\n    provider: voyageai\n"
        )
        app_config = AppConfig.load(str(path))

    select(app_config=app_config).embed(["def add(a, b): return a + b"])

    (request,) = proxy_requests
    assert request["authorization"] == "Bearer proxy-key"
    assert request["body"]["model"] == "voyage-code-3"
    assert request["body"]["input"] == ["def add(a, b): return a + b"]
    assert request["body"]["input_type"] == "document"


# --- The dummy fallback announces itself, and stays distinguishable from
# --- the configured model even when no key is present (BL-35) --------------


def test_configured_embedding_config_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured yardstick is what aimodels.yaml names, key or no key."""
    monkeypatch.delenv("VOYAGE_API_KEY_1", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    config = configured_embedding_config(AppConfig(embedding_models=[model]))

    assert config.provider == "voyageai"
    assert config.model_name == "voyage-code-3"
    assert config.dimension == 1024


def test_configured_embedding_config_is_the_dummy_when_nothing_is_configured() -> None:
    """With no embedding model configured, the dummy is the intent, not a fall."""
    config = configured_embedding_config(AppConfig(embedding_models=[]))

    assert config.provider == "dummy"
    assert config.model_name == "deterministic-sha256"


def test_configured_and_effective_diverge_exactly_when_the_key_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two yardsticks must not collapse into one.

    BL-35: doctor compared the recorded label against the *resolved* provider,
    which degrades to the dummy the same way the build did, so the comparison
    was vacuous. This holds the property that makes the check possible.
    """
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )
    app_config = AppConfig(embedding_models=[model])

    monkeypatch.delenv("VOYAGE_API_KEY_1", raising=False)
    assert configured_embedding_config(app_config) != effective_embedding_config(
        app_config
    )

    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    assert configured_embedding_config(app_config) == effective_embedding_config(
        app_config
    )


def test_dummy_fallback_names_the_model_and_key_it_could_not_use(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Selecting the dummy over a configured model is never silent (BL-35)."""
    monkeypatch.delenv("VOYAGE_API_KEY_1", raising=False)
    model = ModelConfig(
        name="voyage-code-3", provider="voyageai", api_key_env="VOYAGE_API_KEY_1"
    )

    with caplog.at_level("WARNING", logger="knowcode.llm.embedding"):
        create_embedding_provider(app_config=AppConfig(embedding_models=[model]))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "voyage-code-3" in message
    assert "VOYAGE_API_KEY_1" in message
    assert "deterministic-sha256" in message


def test_no_warning_when_the_dummy_is_the_configured_intent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing was asked for, so nothing was lost, so nothing is reported."""
    with caplog.at_level("WARNING", logger="knowcode.llm.embedding"):
        create_embedding_provider(app_config=AppConfig(embedding_models=[]))

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


# --- An entry naming a provider this build cannot construct is stepped over,
# --- not raised (BL-36) ----------------------------------------------------


def _unsupported_model() -> ModelConfig:
    """A configured entry `build_provider_from_model` refuses to construct."""
    return ModelConfig(name="bge-m3", provider="local", api_key_env="KC_LOCAL_KEY")


def test_effective_embedding_config_steps_over_a_provider_it_cannot_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-36: this raised `NotImplementedError` and took `doctor` down with it.

    The key is present, so the entry clears the key gate and reaches the
    build. A provider this release does not implement is a configuration
    error to report, not an exception to propagate into a diagnostic.
    """
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")

    config = effective_embedding_config(
        AppConfig(embedding_models=[_unsupported_model()])
    )

    assert config.provider == "dummy"
    assert config.model_name == "deterministic-sha256"


def test_selection_continues_past_an_unsupported_provider_to_the_next_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remaining models are what "first usable model" was always about."""
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")
    app_config = AppConfig(
        embedding_models=[
            _unsupported_model(),
            ModelConfig(
                name="voyage-code-3",
                provider="voyageai",
                api_key_env="VOYAGE_API_KEY_1",
            ),
        ]
    )

    provider = create_embedding_provider(app_config=app_config)

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.config.model_name == "voyage-code-3"
    assert effective_embedding_config(app_config).provider == "voyageai"


def test_prose_selection_continues_past_an_unsupported_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prose selection loop has the same gate and the same defect."""
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")
    monkeypatch.setenv("VOYAGE_API_KEY_1", "test-key")

    provider = create_prose_embedding_provider(
        app_config=AppConfig(
            prose_embedding_models=[
                _unsupported_model(),
                ModelConfig(
                    name="voyage-code-3",
                    provider="voyageai",
                    api_key_env="VOYAGE_API_KEY_1",
                ),
            ]
        )
    )

    assert isinstance(provider, VoyageAIEmbeddingProvider)
    assert provider.config.model_name == "voyage-code-3"


def test_dummy_fallback_names_the_unsupported_provider_as_the_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Falling back for an unbuildable provider must not report a missing key.

    BL-35 made the substitution loud; the sentence it used says no key is
    set. Here the key *is* set, so that sentence would be a false reading of
    a real config error.
    """
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")

    with caplog.at_level("WARNING", logger="knowcode.llm.embedding"):
        provider = create_embedding_provider(
            app_config=AppConfig(embedding_models=[_unsupported_model()])
        )

    assert isinstance(provider, DummyEmbeddingProvider)
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("bge-m3" in m and "local" in m for m in messages), messages
    assert any("deterministic-sha256" in m for m in messages), messages
    assert not any("No embedding API key is set" in m for m in messages), messages


def test_every_advertised_provider_is_one_this_build_accepts() -> None:
    """The hint BL-36's Config check prints must not name a dead spelling.

    `doctor` tells the user to set `provider` to one of these, so each has to
    survive the function that rejected theirs.
    """
    for provider in SUPPORTED_EMBEDDING_PROVIDERS:
        config = embedding_config_for_model(
            ModelConfig(name="model", provider=provider, api_key_env="KC_TEST_KEY")
        )
        assert config.provider in {"voyageai", "openai"}
