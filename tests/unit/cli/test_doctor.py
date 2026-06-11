"""Unit tests for the doctor CLI command."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from knowcode.data_models import Entity, EntityKind, Location
from knowcode.indexing.indexer import Indexer
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.vector_store import VectorStore


cli_module = importlib.import_module("knowcode.cli.cli")


def _write_config(path: Path) -> None:
    path.write_text(
        """
natural_language_models:
  - name: gemini-test
    provider: google
    api_key_env: KC_TEST_LLM_KEY
embedding_models:
  - name: voyage-code-3
    provider: voyageai
    api_key_env: KC_TEST_EMBED_KEY
config:
  sufficiency_threshold: 0.8
""",
        encoding="utf-8",
    )


def _write_store(path: Path) -> None:
    store = KnowledgeStore()
    entity = Entity(
        id="sample.py::foo",
        kind=EntityKind.FUNCTION,
        name="foo",
        qualified_name="foo",
        location=Location(file_path="sample.py", line_start=1, line_end=2),
        source_code="def foo():\n    return 1\n",
    )
    store.entities[entity.id] = entity
    store.save(path)


def _write_index(path: Path, *, dimension: int = 1024) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": Indexer.SCHEMA_VERSION,
                "embedding": {
                    "provider": "voyageai",
                    "model_name": "voyage-code-3",
                    "dimension": dimension,
                    "normalize": True,
                },
                "chunking": {},
            }
        ),
        encoding="utf-8",
    )
    (path / "chunks.json").write_text(
        json.dumps({"schema_version": Indexer.SCHEMA_VERSION, "chunks": []}),
        encoding="utf-8",
    )
    (path / "vectors.json").write_text(
        json.dumps(
            {
                "schema_version": VectorStore.SCHEMA_VERSION,
                "id_map": {},
                "dimension": dimension,
            }
        ),
        encoding="utf-8",
    )
    (path / "vectors.index").write_bytes(b"placeholder")


@pytest.fixture(autouse=True)
def _api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KC_TEST_LLM_KEY", "test-llm")
    monkeypatch.setenv("KC_TEST_EMBED_KEY", "test-embed")


def test_doctor_passes_with_valid_store_index_and_config(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    # Write rule file first so it is older than store/index rebuilds
    rules_dir = tmp_path / ".agent" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "context.md").write_text("refer to docs/mcp-contract.md", encoding="utf-8")

    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index")

    result = CliRunner().invoke(

        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} == {
        "Config",
        "API keys",
        "Knowledge store",
        "Semantic index",
        "Disk footprint",
        "Agent rules",
        "Supported languages",
        "Freshness",
        "Native dependencies",
        "Optional dependencies",
    }
    assert all(check["status"] == "pass" for check in payload["checks"])



def test_doctor_reports_missing_store_and_index(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    failed = {check["name"]: check for check in payload["checks"] if check["status"] == "fail"}
    assert "Knowledge store" in failed
    assert "Semantic index" in failed
    assert "knowcode build" in failed["Knowledge store"]["hint"]
    assert "knowcode build" in failed["Semantic index"]["hint"]


def test_doctor_fails_on_index_dimension_mismatch(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index", dimension=999)

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    semantic = next(check for check in payload["checks"] if check["name"] == "Semantic index")
    assert semantic["status"] == "fail"
    assert "dimension mismatch" in semantic["message"]


def test_doctor_checks_rules_freshness_and_unsupported_extensions(tmp_path: Path) -> None:
    """Test that doctor command checks agent rules, freshness, and unsupported extensions."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index")

    # Running doctor should include "Agent rules", "Freshness", and "Supported languages" checks.
    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    
    # Verify the checks are present and report correct warning statuses
    checks = {check["name"]: check for check in payload["checks"]}
    assert "Agent rules" in checks
    assert "Supported languages" in checks
    assert "Freshness" in checks

    # Rule file doesn't exist yet, so it should warn
    assert checks["Agent rules"]["status"] == "warn"
    assert checks["Supported languages"]["status"] == "pass"
    assert checks["Freshness"]["status"] == "pass"

    # Now create rule file and an unsupported Go file
    rules_dir = tmp_path / ".agent" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "context.md").write_text("refer to docs/mcp-contract.md", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main", encoding="utf-8")

    result2 = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )
    assert result2.exit_code == 0
    payload2 = json.loads(result2.output)
    checks2 = {check["name"]: check for check in payload2["checks"]}

    # Rule file exists -> pass; Go file exists -> warn
    assert checks2["Agent rules"]["status"] == "pass"
    assert checks2["Supported languages"]["status"] == "warn"
    assert "Go" in checks2["Supported languages"]["message"]


