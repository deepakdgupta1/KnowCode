"""Environment-backed settings for the agent gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


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
        )
