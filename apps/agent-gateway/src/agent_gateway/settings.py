"""Environment-backed settings for the agent gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlparse


_DEFAULT_ALLOWED_TOOL_NAMES: Tuple[str, ...] = (
    "query_context",
    "search",
    "get_context",
    "trace_calls",
)
_DEFAULT_TAGS: Tuple[str, ...] = ("knowcode", "context")



def _split_csv(value: str) -> Tuple[str, ...]:
    items = [part.strip() for part in value.split(",")]
    return tuple(part for part in items if part)



def _to_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback



def _to_float(value: str, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _to_bool(value: str, fallback: bool) -> bool:
    if not isinstance(value, str):
        return fallback

    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return fallback


@dataclass(frozen=True)
class GatewaySettings:
    """Runtime settings loaded from environment variables."""

    knowcode_api_base_url: str
    litellm_base_url: str
    litellm_api_key: str
    default_model: str
    max_tool_rounds: int
    tool_timeout_seconds: float
    openapi_cache_ttl_seconds: int
    allowed_tool_names: Tuple[str, ...]
    default_tags: Tuple[str, ...]
    strict_env_validation: bool

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        allowed_tool_names_raw = os.getenv("AGENT_ALLOWED_TOOL_NAMES", "")
        default_tags_raw = os.getenv("AGENT_DEFAULT_TAGS", "")

        allowed_tool_names = (
            _split_csv(allowed_tool_names_raw)
            if allowed_tool_names_raw.strip()
            else _DEFAULT_ALLOWED_TOOL_NAMES
        )
        default_tags = (
            _split_csv(default_tags_raw) if default_tags_raw.strip() else _DEFAULT_TAGS
        )

        return cls(
            knowcode_api_base_url=os.getenv(
                "KNOWCODE_API_BASE_URL", "http://127.0.0.1:8000"
            ),
            litellm_base_url=os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000"),
            litellm_api_key=os.getenv("LITELLM_API_KEY", "sk-local-proxy"),
            default_model=os.getenv(
                "AGENT_DEFAULT_MODEL", "gemini/gemini-3-flash-preview"
            ),
            max_tool_rounds=max(1, _to_int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "4"), 4)),
            tool_timeout_seconds=max(
                1.0,
                _to_float(
                    os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "30.0"),
                    30.0,
                ),
            ),
            openapi_cache_ttl_seconds=max(
                0,
                _to_int(
                    os.getenv("AGENT_OPENAPI_CACHE_TTL_SECONDS", "300"),
                    300,
                ),
            ),
            allowed_tool_names=allowed_tool_names,
            default_tags=default_tags,
            strict_env_validation=_to_bool(
                os.getenv("AGENT_STRICT_ENV_VALIDATION", "false"),
                False,
            ),
        )

    def validate(self) -> None:
        """Validate settings for production-like deployments."""
        if not self.strict_env_validation:
            return

        if not self.litellm_api_key.strip() or self.litellm_api_key == "sk-local-proxy":
            raise ValueError(
                "LITELLM_API_KEY must be set to a non-default value when "
                "AGENT_STRICT_ENV_VALIDATION=true"
            )

        if self._is_loopback_url(self.knowcode_api_base_url):
            raise ValueError(
                "KNOWCODE_API_BASE_URL must not point to localhost when "
                "AGENT_STRICT_ENV_VALIDATION=true"
            )

        if self._is_loopback_url(self.litellm_base_url):
            raise ValueError(
                "LITELLM_BASE_URL must not point to localhost when "
                "AGENT_STRICT_ENV_VALIDATION=true"
            )

    def _is_loopback_url(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname in {"localhost", "127.0.0.1", "::1"}
