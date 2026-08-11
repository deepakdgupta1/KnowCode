"""Tests for the core routing-policy artifact consumer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.routing_policy import load_routing_policy
from knowcode.service import KnowCodeService


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_blessed_artifact(root: Path) -> Path:
    source = root / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    artifact = {
        "schema_version": "1.1.0",
        "verification_kind": "independent_machine_adjudication",
        "status": "blessed",
        "subject": {
            "product": "knowcode",
            "product_version": "0.2.3",
            "source_revision": "1" * 40,
        },
        "internal_dataset": {
            "name": "locked-holdout-v1",
            "partition": "locked_holdout",
            "sha256": "2" * 64,
            "case_count": 29,
        },
        "external_datasets": {
            name: {
                "version": version,
                "revision": revision,
                "manifest_sha256": digest,
                "language": "python",
                "case_count": 30,
            }
            for name, version, revision, digest in (
                ("repobench-r", "archive/v0", "3" * 40, "4" * 64),
                ("repoqa", "2024-06-23", "5" * 40, "6" * 64),
            )
        },
        "routing_policy": {
            "status": "blessed",
            "sufficiency_threshold": 0.91,
            "local_answer_task_types": ["locate"],
        },
        "judges": [
            {"provider": "openai", "model": "judge-a", "prompt_sha256": "a" * 64},
            {"provider": "google", "model": "judge-b", "prompt_sha256": "b" * 64},
        ],
        "metric_floors": {
            "routing_quality_floor": 0.9,
            "bm25_noninferiority_margin": 0.02,
            "judge_macro_f1": 0.95,
        },
        "canary_results": {
            "passed": True,
            "critical_false_accepts": 0,
            "macro_f1": 1.0,
        },
        "evidence_hashes": {
            "internal_report_sha256": "c" * 64,
            "external_report_sha256": "d" * 64,
            "downstream_report_sha256": "e" * 64,
        },
        "source_hashes": {"src/example.py": _sha256(source)},
    }
    path = root / "machine-verification.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def test_blessed_policy_requires_an_explicit_artifact_digest(tmp_path: Path) -> None:
    artifact = _write_blessed_artifact(tmp_path)

    with pytest.raises(ValueError, match="expected SHA-256"):
        load_routing_policy(artifact, source_root=tmp_path, expected_sha256=None)


def test_blessed_policy_rejects_an_unexpected_artifact_digest(tmp_path: Path) -> None:
    artifact = _write_blessed_artifact(tmp_path)

    with pytest.raises(ValueError, match="checksum mismatch"):
        load_routing_policy(
            artifact,
            source_root=tmp_path,
            expected_sha256="f" * 64,
        )


def test_service_resolves_policy_sources_from_store_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    artifact = _write_blessed_artifact(repository)
    nested_cwd = repository / "nested" / "directory"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.setenv("KNOWCODE_ROUTING_POLICY_ARTIFACT", str(artifact))
    monkeypatch.setenv("KNOWCODE_ROUTING_POLICY_SHA256", _sha256(artifact))
    config = AppConfig.default()

    service = KnowCodeService(store_path=repository, app_config=config)

    assert service.app_config.local_answer_task_types == ["locate"]
    assert service.app_config.sufficiency_threshold == 0.91


def test_blessed_policy_rejects_source_drift(tmp_path: Path) -> None:
    artifact = _write_blessed_artifact(tmp_path)
    (tmp_path / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source drift"):
        load_routing_policy(
            artifact,
            source_root=tmp_path,
            expected_sha256=_sha256(artifact),
        )


def test_blessed_policy_rejects_a_different_product_version(tmp_path: Path) -> None:
    artifact = _write_blessed_artifact(tmp_path)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["subject"]["product_version"] = "999.0.0"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="different KnowCode version"):
        load_routing_policy(
            artifact,
            source_root=tmp_path,
            expected_sha256=_sha256(artifact),
        )


def test_unblessed_policy_remains_fail_closed_without_provenance_fields(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "machine-verification.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "verification_kind": "independent_machine_adjudication",
                "status": "unblessed",
                "routing_policy": {
                    "status": "unblessed",
                    "sufficiency_threshold": 0.5,
                    "local_answer_task_types": ["locate"],
                },
            }
        ),
        encoding="utf-8",
    )

    policy = load_routing_policy(
        artifact,
        source_root=tmp_path,
        expected_sha256=_sha256(artifact),
    )

    assert policy is None
