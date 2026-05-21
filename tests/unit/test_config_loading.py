"""Unit tests for configuration loading and validation behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from knowcode.config import AppConfig


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_non_strict_warns_on_unknown_top_level_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Unknown top-level keys should warn (not fail) in non-strict mode."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
unknown_top_level: true
""",
    )

    with caplog.at_level(logging.WARNING, logger="knowcode.config"):
        cfg = AppConfig.load(str(config_file))

    assert cfg.models and cfg.models[0].name == "gemini-2.0-flash-lite"
    assert any("Unknown top-level config keys" in rec.message for rec in caplog.records)


def test_load_non_strict_warns_on_unknown_config_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Unknown nested config keys should warn (not fail) in non-strict mode."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
config:
  sufficiency_threshold: 0.75
  unsupported_flag: true
""",
    )

    with caplog.at_level(logging.WARNING, logger="knowcode.config"):
        cfg = AppConfig.load(str(config_file))

    assert cfg.sufficiency_threshold == 0.75
    assert any("Unknown config keys" in rec.message for rec in caplog.records)


def test_load_strict_rejects_unknown_keys(tmp_path: Path) -> None:
    """Strict mode should reject unknown keys instead of warning."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
unknown_top_level: true
""",
    )

    with pytest.raises(ValueError, match="Unknown top-level config keys"):
        AppConfig.load(str(config_file), strict=True)


def test_load_strict_allows_eval_models_section(tmp_path: Path) -> None:
    """The checked-in eval_models section is intentional config metadata."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
eval_models:
  - name: voyage-code-3
    provider: voyageai
""",
    )

    cfg = AppConfig.load(str(config_file), strict=True)

    assert cfg.models[0].name == "gemini-2.0-flash-lite"


def test_load_strict_rejects_invalid_root_type(tmp_path: Path) -> None:
    """Strict mode should reject invalid YAML root types."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
- not
- a
- mapping
""",
    )

    with pytest.raises(ValueError, match="Config root must be a YAML mapping"):
        AppConfig.load(str(config_file), strict=True)


def test_load_non_strict_invalid_file_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-strict mode should warn and return defaults on invalid config."""
    config_file = tmp_path / "aimodels.yaml"
    _write(config_file, "config: [")

    with caplog.at_level(logging.WARNING, logger="knowcode.config"):
        cfg = AppConfig.load(str(config_file))

    assert cfg.models  # default models loaded
    assert any("Failed to load config" in rec.message for rec in caplog.records)
