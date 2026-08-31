"""The single route outbound model traffic is required to take."""

from __future__ import annotations

import os

#: Address of the local LiteLLM proxy every OpenAI-compatible request
#: targets by default, so key handling, model routing and rate limits live
#: in one place instead of one client at a time. An unset override used to
#: mean ``base_url=None``, which silently aimed the OpenAI client at
#: ``api.openai.com`` with a provider key it cannot accept.
DEFAULT_LITELLM_BASE_URL = "http://127.0.0.1:4000"


def litellm_base_url(env: dict[str, str] | None = None) -> str:
    """Return the proxy address, honouring an explicit override.

    Args:
        env: Environment to read ``LITELLM_BASE_URL`` from; defaults to
            ``os.environ``.

    Returns:
        The override when set and non-empty, else the default proxy address.
    """
    source = os.environ if env is None else env
    return source.get("LITELLM_BASE_URL") or DEFAULT_LITELLM_BASE_URL
