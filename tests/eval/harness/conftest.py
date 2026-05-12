"""pytest configuration for the retrieval-quality eval harness."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
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
        help="Skip the SHA guard (local exploration only; never in CI).",
    )
