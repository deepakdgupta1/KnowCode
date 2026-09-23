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


#: Tests that shell out to ``git commit`` must not depend on the machine's own
#: identity. A CI runner whose hostname has no domain gives git nothing to
#: auto-detect, and git then refuses the commit with exit 128.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "KnowCode Tests",
    "GIT_AUTHOR_EMAIL": "tests@knowcode.invalid",
    "GIT_COMMITTER_NAME": "KnowCode Tests",
    "GIT_COMMITTER_EMAIL": "tests@knowcode.invalid",
}


def pytest_configure(config: pytest.Config) -> None:
    """Configure repository-wide settings before tests run."""
    import os

    os.environ["KNOWCODE_TESTING"] = "1"
    for variable, value in _GIT_IDENTITY.items():
        os.environ.setdefault(variable, value)
