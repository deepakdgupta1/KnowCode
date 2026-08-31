"""The single route outbound model traffic is required to take."""

#: Address of the local LiteLLM proxy that OpenAI-compatible chat traffic
#: targets by default, so key handling, model routing and rate limits live
#: in one place instead of one client at a time. An unset ``GLM_BASE_URL``
#: used to mean ``base_url=None``, which silently aimed the OpenAI client
#: at ``api.openai.com`` with a provider key it cannot accept. Embeddings
#: join this route only when ``VOYAGE_BASE_URL`` names a proxy address;
#: the direct VoyageAI client remains their default.
DEFAULT_LITELLM_BASE_URL = "http://127.0.0.1:4000"
