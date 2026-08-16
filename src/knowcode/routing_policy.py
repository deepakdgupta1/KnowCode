"""Validation and loading for runtime local-answer routing policies.

This module is intentionally part of the KnowCode runtime. The separate
``knowcode-evals`` project produces policy artifacts; KnowCode only consumes
an explicitly checksum-pinned artifact and enforces its decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


ARTIFACT_SCHEMA_VERSION = "1.1.0"
SUPPORTED_TASK_TYPES = {
    "explain",
    "debug",
    "extend",
    "review",
    "locate",
    "general",
}
REQUIRED_EXTERNAL_DATASETS = {"repobench-r", "repoqa"}


@dataclass(frozen=True)
class RoutingPolicy:
    """A validated runtime routing decision from the external evaluator."""

    sufficiency_threshold: float
    local_answer_task_types: tuple[str, ...]
    routing_quality_floor: float
    artifact_sha256: str


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _repository_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Artifact source paths must be non-empty strings.")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"Artifact source path must be repository-relative: {value!r}")
    return str(path)


def _verify_source_hashes(artifact: Mapping[str, Any], source_root: Path) -> None:
    expected = artifact.get("source_hashes")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Blessed artifact contains no source hashes.")
    drifted: list[str] = []
    for raw_relative, raw_digest in expected.items():
        relative = _repository_path(raw_relative)
        if not _is_sha256(raw_digest):
            raise ValueError(f"Malformed source hash for {relative}.")
        path = source_root / relative
        if not path.is_file():
            drifted.append(relative)
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual.lower() != str(raw_digest).lower():
            drifted.append(relative)
    if drifted:
        raise ValueError("Machine-verification source drift: " + ", ".join(drifted))


def _validate_subject(artifact: Mapping[str, Any]) -> None:
    subject = artifact.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("Blessed artifact lacks a product subject.")
    if subject.get("product") != "knowcode":
        raise ValueError("Blessed artifact targets a different product.")
    product_version = subject.get("product_version")
    if not isinstance(product_version, str) or not product_version:
        raise ValueError("Blessed artifact lacks the KnowCode version.")
    try:
        installed_version = version("knowcode")
    except PackageNotFoundError as exc:
        raise ValueError("Cannot determine the installed KnowCode version.") from exc
    if product_version != installed_version:
        raise ValueError("Blessed artifact targets a different KnowCode version.")
    revision = subject.get("source_revision")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or not all(character in "0123456789abcdef" for character in revision.lower())
    ):
        raise ValueError("Blessed artifact lacks a valid product source revision.")


def _validate_internal_dataset(artifact: Mapping[str, Any]) -> None:
    dataset = artifact.get("internal_dataset")
    if not isinstance(dataset, dict):
        raise ValueError("Blessed artifact lacks its locked holdout identity.")
    if (
        not isinstance(dataset.get("name"), str)
        or not dataset["name"]
        or dataset.get("partition") != "locked_holdout"
        or not _is_sha256(dataset.get("sha256"))
        or not isinstance(dataset.get("case_count"), int)
        or isinstance(dataset.get("case_count"), bool)
        or int(dataset["case_count"]) < 29
    ):
        raise ValueError(
            "Blessed artifact contains an invalid locked holdout identity."
        )


def _validate_external_datasets(artifact: Mapping[str, Any]) -> None:
    datasets = artifact.get("external_datasets")
    if not isinstance(datasets, dict) or set(datasets) != REQUIRED_EXTERNAL_DATASETS:
        raise ValueError("Blessed artifact lacks the two blocking external datasets.")
    for name, dataset in datasets.items():
        if not isinstance(dataset, dict):
            raise ValueError(f"External dataset identity is malformed: {name}.")
        if (
            dataset.get("language") != "python"
            or not isinstance(dataset.get("version"), str)
            or not dataset["version"]
            or not isinstance(dataset.get("revision"), str)
            or not dataset["revision"]
            or not _is_sha256(dataset.get("manifest_sha256"))
            or not isinstance(dataset.get("case_count"), int)
            or isinstance(dataset.get("case_count"), bool)
            or int(dataset["case_count"]) < 1
        ):
            raise ValueError(f"External dataset identity is incomplete: {name}.")


def _validate_judges(artifact: Mapping[str, Any]) -> None:
    judges = artifact.get("judges")
    if not isinstance(judges, list) or len(judges) != 2:
        raise ValueError("Blessed artifact requires two judge identities.")
    providers: set[str] = set()
    for judge in judges:
        if not isinstance(judge, dict):
            raise ValueError("Blessed artifact contains a malformed judge identity.")
        provider = judge.get("provider")
        if (
            provider not in {"openai", "google"}
            or not isinstance(judge.get("model"), str)
            or not judge["model"]
            or not _is_sha256(judge.get("prompt_sha256"))
        ):
            raise ValueError("Blessed artifact contains an incomplete judge identity.")
        providers.add(str(provider))
    if providers != {"openai", "google"}:
        raise ValueError(
            "Blessed artifact requires independent OpenAI and Google judges."
        )


def _validate_evidence(artifact: Mapping[str, Any]) -> float:
    floors = artifact.get("metric_floors")
    if not isinstance(floors, dict):
        raise ValueError("Blessed artifact lacks metric floors.")
    routing_floor = floors.get("routing_quality_floor")
    bm25_margin = floors.get("bm25_noninferiority_margin")
    judge_floor = floors.get("judge_macro_f1")
    if (
        not isinstance(routing_floor, (int, float))
        or isinstance(routing_floor, bool)
        or float(routing_floor) < 0.9
        or not isinstance(bm25_margin, (int, float))
        or isinstance(bm25_margin, bool)
        or not 0 <= float(bm25_margin) <= 0.02
        or not isinstance(judge_floor, (int, float))
        or isinstance(judge_floor, bool)
        or float(judge_floor) < 0.95
    ):
        raise ValueError("Blessed artifact weakens required metric floors.")

    canaries = artifact.get("canary_results")
    if (
        not isinstance(canaries, dict)
        or canaries.get("passed") is not True
        or canaries.get("critical_false_accepts") != 0
        or not isinstance(canaries.get("macro_f1"), (int, float))
        or isinstance(canaries.get("macro_f1"), bool)
        or float(canaries["macro_f1"]) < float(judge_floor)
    ):
        raise ValueError("Blessed artifact lacks passing canary evidence.")

    evidence_hashes = artifact.get("evidence_hashes")
    required_hashes = {
        "internal_report_sha256",
        "external_report_sha256",
        "downstream_report_sha256",
    }
    if (
        not isinstance(evidence_hashes, dict)
        or set(evidence_hashes) != required_hashes
        or any(not _is_sha256(value) for value in evidence_hashes.values())
    ):
        raise ValueError("Blessed artifact lacks valid evidence hashes.")
    return float(routing_floor)


def load_routing_policy(
    path: Path,
    *,
    source_root: Path,
    expected_sha256: str | None,
) -> RoutingPolicy | None:
    """Load a checksum-pinned policy artifact or return ``None`` if unblessed."""
    if not _is_sha256(expected_sha256):
        raise ValueError("Routing policy requires an explicit expected SHA-256.")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Invalid machine-verification artifact: {path}") from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256.lower() != str(expected_sha256).lower():
        raise ValueError("Routing policy artifact checksum mismatch.")
    try:
        artifact = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid machine-verification artifact: {path}") from exc
    if not isinstance(artifact, dict) or artifact.get("verification_kind") != (
        "independent_machine_adjudication"
    ):
        raise ValueError("Invalid machine-verification artifact kind.")
    policy = artifact.get("routing_policy")
    if not isinstance(policy, dict):
        raise ValueError("Machine-verification artifact lacks a routing policy.")
    if policy.get("status") != "blessed" or artifact.get("status") != "blessed":
        return None
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("Unsupported blessed machine-verification artifact schema.")

    _validate_subject(artifact)
    _validate_internal_dataset(artifact)
    _validate_external_datasets(artifact)
    _validate_judges(artifact)
    routing_floor = _validate_evidence(artifact)
    _verify_source_hashes(artifact, source_root)

    task_types = policy.get("local_answer_task_types")
    threshold = policy.get("sufficiency_threshold")
    if not isinstance(task_types, list) or not all(
        isinstance(value, str) for value in task_types
    ):
        raise ValueError("Machine-verification allowlist is malformed.")
    unsupported = sorted(set(task_types) - SUPPORTED_TASK_TYPES)
    if unsupported:
        raise ValueError(
            "Machine-verification allowlist contains unsupported task types: "
            + ", ".join(unsupported)
        )
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not 0.5 <= float(threshold) <= 1
    ):
        raise ValueError("Machine-verification threshold is malformed.")
    return RoutingPolicy(
        sufficiency_threshold=float(threshold),
        local_answer_task_types=tuple(dict.fromkeys(task_types)),
        routing_quality_floor=routing_floor,
        artifact_sha256=actual_sha256,
    )
