"""Unit tests for gateway environment settings."""

from __future__ import annotations

import pytest

from agent_gateway.settings import GatewaySettings


def _build_settings(**overrides: object) -> GatewaySettings:
    base = {
        "knowcode_api_base_url": "http://127.0.0.1:8000",
        "litellm_base_url": "http://127.0.0.1:4000",
        "litellm_api_key": "sk-local-proxy",
        "default_model": "gemini/gemini-3-flash-preview",
        "max_tool_rounds": 4,
        "tool_timeout_seconds": 30.0,
        "openapi_cache_ttl_seconds": 300,
        "allowed_tool_names": ("query_context", "search"),
        "default_tags": ("knowcode",),
        "strict_env_validation": False,
    }
    base.update(overrides)
    return GatewaySettings(**base)


def test_validate_passes_when_strict_validation_disabled() -> None:
    settings = _build_settings(strict_env_validation=False)
    settings.validate()


def test_validate_fails_with_default_litellm_api_key_in_strict_mode() -> None:
    settings = _build_settings(strict_env_validation=True)

    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        settings.validate()


def test_validate_fails_with_loopback_urls_in_strict_mode() -> None:
    settings = _build_settings(
        strict_env_validation=True,
        litellm_api_key="sk-real-key",
        knowcode_api_base_url="http://localhost:8000",
        litellm_base_url="http://litellm.internal:4000",
    )

    with pytest.raises(ValueError, match="KNOWCODE_API_BASE_URL"):
        settings.validate()

