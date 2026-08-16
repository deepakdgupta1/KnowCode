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


def pytest_configure(config: pytest.Config) -> None:
    """Configure repository-wide settings before tests run."""
    import os

    os.environ["KNOWCODE_TESTING"] = "1"
