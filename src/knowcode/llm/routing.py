"""The single route outbound model traffic is required to take."""

#: Address of the local LiteLLM proxy that outbound model traffic targets by
#: default, so key handling, model routing and rate limits live
#: in one place instead of one client at a time. An unset ``GLM_BASE_URL``
#: used to mean ``base_url=None``, which silently aimed the OpenAI client
#: at ``api.openai.com`` with a provider key it cannot accept. An unset
#: ``VOYAGE_BASE_URL`` also uses this route, so embeddings cannot silently
#: bypass the proxy.
DEFAULT_LITELLM_BASE_URL = "http://127.0.0.1:4000"
