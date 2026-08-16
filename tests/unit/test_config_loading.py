"""Unit tests for configuration loading and validation behavior."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from knowcode.config import AppConfig


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_non_strict_warns_on_unknown_top_level_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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


def test_load_non_strict_warns_on_unknown_config_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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


def test_load_parses_fail_closed_routing_policy(tmp_path: Path) -> None:
    """A YAML allowlist cannot bypass the machine-verification artifact."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
config:
  sufficiency_threshold: 0.87
  local_answer_task_types: [locate, explain]
  routing_quality_floor: 0.9
""",
    )

    cfg = AppConfig.load(str(config_file), strict=True)

    assert cfg.sufficiency_threshold == 0.87
    assert cfg.local_answer_task_types == []
    assert cfg.routing_quality_floor == 0.9


def test_default_routing_policy_is_fail_closed() -> None:
    cfg = AppConfig.default()

    assert cfg.local_answer_task_types == []
    assert cfg.routing_quality_floor == 0.9


def test_unblessed_machine_artifact_cannot_enable_routing(tmp_path: Path) -> None:
    artifact = tmp_path / "machine-verification.json"
    artifact.write_text(
        """
{
  "schema_version": "1.0.0",
  "verification_kind": "independent_machine_adjudication",
  "status": "unblessed",
  "routing_policy": {
    "status": "unblessed",
    "sufficiency_threshold": 0.5,
    "local_answer_task_types": ["locate"]
  }
}
""",
        encoding="utf-8",
    )
    config = AppConfig(local_answer_task_types=["locate"])
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    config.apply_machine_verification_artifact(
        artifact,
        source_root=tmp_path,
        expected_sha256=digest,
    )

    assert config.local_answer_task_types == []


@pytest.mark.parametrize(
    "expected_sha256",
    [None, "f" * 64],
    ids=["missing_checksum_env", "wrong_checksum"],
)
def test_unverifiable_artifact_fails_closed_without_raising(
    tmp_path: Path,
    expected_sha256: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unverifiable artifact must fail closed, not crash the service.

    A missing ``KNOWCODE_ROUTING_POLICY_SHA256`` (operator forgot to set it)
    and a checksum mismatch are both operator/integrity problems whose secure
    resolution is identical: disable local answering and keep running LLM-only.
    Neither should propagate out of ``apply_machine_verification_artifact``.
    """
    artifact = tmp_path / "machine-verification.json"
    artifact.write_text('{"status": "blessed"}', encoding="utf-8")
    config = AppConfig(local_answer_task_types=["locate"])

    with caplog.at_level(logging.WARNING, logger="knowcode.config"):
        config.apply_machine_verification_artifact(
            artifact,
            source_root=tmp_path,
            expected_sha256=expected_sha256,
        )

    assert config.local_answer_task_types == []
    assert any("local answering disabled" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    ("config_yaml", "message"),
    [
        ("local_answer_task_types: locate", "must be a list"),
        ("local_answer_task_types: [locate, unknown]", "Unsupported task type"),
        ("routing_quality_floor: 1.1", "between 0 and 1"),
    ],
)
def test_load_rejects_invalid_routing_policy(
    tmp_path: Path,
    config_yaml: str,
    message: str,
) -> None:
    config_file = tmp_path / "aimodels.yaml"
    _write(config_file, f"config:\n  {config_yaml}\n")

    with pytest.raises(ValueError, match=message):
        AppConfig.load(str(config_file), strict=True)


@pytest.mark.parametrize(
    ("config_yaml", "message"),
    [
        ("hybrid_alpha: 1.5", "between 0 and 1"),
        ("hybrid_alpha: -0.1", "between 0 and 1"),
        ("hybrid_alpha: true", "must be a number"),
        ("reranker_top_k_multiplier: 0", "between 1 and 100"),
        ("reranker_top_k_multiplier: -1", "between 1 and 100"),
        ("reranker_top_k_multiplier: true", "must be an integer"),
    ],
)
def test_load_rejects_invalid_tuning_knobs(
    tmp_path: Path,
    config_yaml: str,
    message: str,
) -> None:
    """hybrid_alpha and reranker_top_k_multiplier must be range-checked."""
    config_file = tmp_path / "aimodels.yaml"
    _write(config_file, f"config:\n  {config_yaml}\n")

    with pytest.raises(ValueError, match=message):
        AppConfig.load(str(config_file), strict=True)


def test_load_defaults_hybrid_alpha_to_0_2(tmp_path: Path) -> None:
    """Omitting hybrid_alpha yields the sparse-heavy 0.2 default everywhere."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
""",
    )

    cfg = AppConfig.load(str(config_file))

    assert cfg.hybrid_alpha == 0.2


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


def test_load_strict_parses_prose_embedding_models(tmp_path: Path) -> None:
    """Prose embedding models should be independently configurable."""
    config_file = tmp_path / "aimodels.yaml"
    _write(
        config_file,
        """
natural_language_models:
  - name: gemini-2.0-flash-lite
prose_embedding_models:
  - name: voyage-3-large
    provider: voyageai
    api_key_env: VOYAGE_API_KEY_PROSE
    tokens_free_tier_limit: 200000000
""",
    )

    cfg = AppConfig.load(str(config_file), strict=True)

    assert len(cfg.prose_embedding_models) == 1
    assert cfg.prose_embedding_models[0].name == "voyage-3-large"
    assert cfg.prose_embedding_models[0].api_key_env == "VOYAGE_API_KEY_PROSE"
    assert cfg.prose_embedding_models[0].tokens_free_tier_limit == 200000000


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


def test_load_non_strict_invalid_file_raises_error(tmp_path: Path) -> None:
    """Non-strict mode should still raise on invalid config to prevent silent failures."""
    config_file = tmp_path / "aimodels.yaml"
    _write(config_file, "config: [")

    with pytest.raises(ValueError, match="Failed to load config from"):
        AppConfig.load(str(config_file))
