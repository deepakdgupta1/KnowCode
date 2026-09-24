"""Repository-wide pytest configuration."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register options used by repository-level test runs."""
    parser.addoption(
        "--save-baseline",
        action="store_true",
        default=False,
        help="Write evaluation results to baseline_v1.0.json after the run.",
    )
    parser.addoption(
        "--allow-drift",
        action="store_true",
        default=False,
        help="Skip the eval golden SHA guard for local exploration.",
    )


#: Credentials and routes a developer's shell may export for real use. Tests
#: that need one set it themselves, so a suite run from such a shell must not
#: reach the LiteLLM proxy or a provider. That would bill real calls and fail
#: every test that expects the offline embedder.
_CREDENTIAL_PREFIXES = (
    "LITELLM_",
    "GLM_",
    "VOYAGE_",
    "OPENAI_",
    "OPENROUTER_",
    "GOOGLE_API_KEY",
)


def pytest_configure(config: pytest.Config) -> None:
    """Configure repository-wide settings before tests run."""
    import os

    os.environ["KNOWCODE_TESTING"] = "1"
    for name in [name for name in os.environ if name.startswith(_CREDENTIAL_PREFIXES)]:
        del os.environ[name]
