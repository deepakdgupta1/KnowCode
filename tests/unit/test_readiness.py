"""Tests for shared readiness metadata."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from knowcode import __version__
from knowcode import readiness


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_package_version_matches_pyproject() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, flags=re.MULTILINE)

    assert match is not None
    assert __version__ == match.group(1)


def test_pyproject_all_extra_includes_ideal_setup_dependencies() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^all = \[(.*?)^\]", text, flags=re.MULTILINE | re.DOTALL)

    assert match is not None
    all_extra = match.group(1)
    assert "mcp>=1.0.0" in all_extra
    assert "voyageai>=0.2.0" in all_extra


def test_missing_features_are_deduped_by_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "find_spec", lambda _module: None)

    missing = readiness.missing_features(("llm", "llm"))

    assert len(missing) == 1
    assert missing[0].feature.key == "llm"
    assert set(missing[0].modules) == {
        "openai",
        "google.genai",
        "google.api_core",
    }


def test_build_install_command_uses_ideal_setup_target() -> None:
    command = readiness.build_install_command(
        upgrade=True,
        user_install=True,
        python_executable="python",
    )

    assert command == [
        "python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--user",
        readiness.IDEAL_SETUP_TARGET,
    ]
