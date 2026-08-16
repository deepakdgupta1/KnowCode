"""Tests for deterministic documentation synthesis."""

from __future__ import annotations

import json
from pathlib import Path

from knowcode.analysis.documentation_synthesizer import DocumentationSynthesizer
from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.storage.knowledge_store import KnowledgeStore


def _sample_store() -> KnowledgeStore:
    store = KnowledgeStore()

    module = Entity(
        id="pkg/service.py::module",
        kind=EntityKind.MODULE,
        name="service",
        qualified_name="pkg.service",
        location=Location("pkg/service.py", 1, 1),
        docstring="Coordinates background jobs.",
        metadata={"language": "python", "content_hash": "module-hash"},
    )
    run = Entity(
        id="pkg/service.py::run",
        kind=EntityKind.FUNCTION,
        name="run",
        qualified_name="pkg.service.run",
        location=Location("pkg/service.py", 3, 7),
        signature="def run(config: dict) -> None",
        docstring="Run the configured job.",
        metadata={
            "language": "python",
            "content_hash": "run-hash",
            "behavior": {
                "side_effect_class": "io",
                "side_effects": ["io"],
                "reads": ["config"],
                "writes": [],
                "calls": ["Worker.start"],
                "confidence": 0.9,
            },
        },
    )
    worker = Entity(
        id="pkg/service.py::Worker",
        kind=EntityKind.CLASS,
        name="Worker",
        qualified_name="pkg.service.Worker",
        location=Location("pkg/service.py", 10, 20),
        signature="class Worker",
        docstring="Executes queued work.",
        metadata={"language": "python", "content_hash": "worker-hash"},
    )
    other = Entity(
        id="pkg/other.py::helper",
        kind=EntityKind.FUNCTION,
        name="helper",
        qualified_name="pkg.other.helper",
        location=Location("pkg/other.py", 1, 3),
        signature="def helper() -> int",
        metadata={"language": "python", "content_hash": "helper-hash"},
    )

    store.entities = {
        module.id: module,
        run.id: run,
        worker.id: worker,
        other.id: other,
    }
    store.relationships = [
        Relationship(module.id, run.id, RelationshipKind.CONTAINS),
        Relationship(module.id, worker.id, RelationshipKind.CONTAINS),
        Relationship(run.id, worker.id, RelationshipKind.CALLS),
        Relationship(other.id, run.id, RelationshipKind.CALLS),
    ]
    return store


def test_generate_multi_level_docs_with_manifest() -> None:
    bundle = DocumentationSynthesizer(
        _sample_store(),
        generated_at="2026-06-10T15:30:00Z",
    ).generate()

    assert set(bundle.documents) == {
        "architecture.md",
        "index.md",
        "modules/pkg__other.py.md",
        "modules/pkg__service.py.md",
    }
    assert "# Codebase Documentation" in bundle.documents["index.md"]
    assert (
        "[pkg/service.py](modules/pkg__service.py.md)" in bundle.documents["index.md"]
    )
    assert "# Architecture Overview" in bundle.documents["architecture.md"]
    assert "- function: 2" in bundle.documents["architecture.md"]

    module_doc = bundle.documents["modules/pkg__service.py.md"]
    assert "# Module: `pkg/service.py`" in module_doc
    assert "Coordinates background jobs." in module_doc
    assert "### function `pkg.service.run`" in module_doc
    assert "Signature: `def run(config: dict) -> None`" in module_doc
    assert "Calls: `pkg.service.Worker`" in module_doc
    assert "Called by: `pkg.other.helper`" in module_doc
    assert "Side effect class: `io`" in module_doc

    manifest = bundle.manifest
    assert manifest["schema_version"] == 1
    assert manifest["generated_at"] == "2026-06-10T15:30:00Z"
    assert manifest["documents"] == sorted(bundle.documents)
    assert manifest["source_files"]["pkg/service.py"]["entity_count"] == 3
    assert manifest["entity_hashes"]["pkg/service.py::run"] == "run-hash"


def test_write_outputs_documents_and_manifest(tmp_path: Path) -> None:
    bundle = DocumentationSynthesizer(
        _sample_store(),
        generated_at="2026-06-10T15:30:00Z",
    ).write(tmp_path)

    assert (tmp_path / "index.md").read_text(encoding="utf-8") == bundle.documents[
        "index.md"
    ]
    assert (tmp_path / "architecture.md").exists()
    assert (tmp_path / "modules" / "pkg__service.py.md").exists()

    manifest = json.loads(
        (tmp_path / "documentation_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["documents"] == sorted(bundle.documents)
