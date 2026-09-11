"""Unit tests for the doctor CLI command."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import pytest

from knowcode.data_models import Entity, EntityKind, Location
from knowcode.indexing import generations
from knowcode.indexing.indexer import Indexer
from knowcode import readiness
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.lancedb_vector_store import LanceDBVectorStore
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.storage.vector_store import VectorStore


cli_module = importlib.import_module("knowcode.cli.cli")
doctor_module = importlib.import_module("knowcode.doctor")


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


def _write_sqlite_store(path: Path) -> None:
    store = SqliteKnowledgeStore(path)
    store.add_entity(
        Entity(
            id="sample.py::foo",
            kind=EntityKind.FUNCTION,
            name="foo",
            qualified_name="foo",
            location=Location(file_path="sample.py", line_start=1, line_end=2),
            source_code="def foo():\n    return 1\n",
        )
    )
    store.close()


def _embedding_block(dimension: int, provider: str, model_name: str) -> dict:
    return {
        "provider": provider,
        "model_name": model_name,
        "dimension": dimension,
        "normalize": True,
    }


def _write_index(
    path: Path,
    *,
    dimension: int = 1024,
    backend: str | None = None,
    provider: str = "voyageai",
    model_name: str = "voyage-code-3",
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": Indexer.SCHEMA_VERSION,
                "embedding": _embedding_block(dimension, provider, model_name),
                "chunking": {},
            }
        ),
        encoding="utf-8",
    )
    (path / "chunks.json").write_text(
        json.dumps({"schema_version": Indexer.SCHEMA_VERSION, "chunks": []}),
        encoding="utf-8",
    )
    vector_metadata = {
        "schema_version": (
            LanceDBVectorStore.SCHEMA_VERSION
            if backend == "lancedb"
            else VectorStore.SCHEMA_VERSION
        ),
        "id_map": {},
        "dimension": dimension,
    }
    if backend:
        vector_metadata["backend"] = backend
    (path / "vectors.json").write_text(
        json.dumps(vector_metadata),
        encoding="utf-8",
    )
    if backend == "lancedb":
        (path / "vectors.lancedb").mkdir()
        (path / "vectors.lancedb" / "vectors.lance").write_bytes(b"placeholder")
    else:
        (path / "vectors.index").write_bytes(b"placeholder")


def _publish_generation(
    index_root: Path,
    *,
    dimension: int = 1024,
    backend: str | None = None,
    kind: str = generations.KIND_FULL,
    builder: dict | None = None,
    provider: str = "voyageai",
    model_name: str = "voyage-code-3",
) -> Path:
    """Publish a complete generation the way a real build does (Step 14)."""
    with generations.staged_generation(index_root) as staging:
        _write_sqlite_store(staging.path / "knowledge.db")
        chunk_ids: list[str] = []
        if kind == generations.KIND_FULL:
            _write_index(
                staging.path,
                dimension=dimension,
                backend=backend,
                provider=provider,
                model_name=model_name,
            )
            _write_chunks_db(staging.path / "chunks.db")

        manifest = generations.build_manifest(
            staging.path,
            generation_id=staging.generation_id,
            kind=kind,
            entity_ids=["sample.py::foo"],
            relationship_count=0,
            chunk_ids=chunk_ids,
            vector_count=0,
            embedding=_embedding_block(dimension, provider, model_name),
            vector={"backend": backend or "faiss", "dimension": dimension},
            builder=builder,
        )
        published = generations.publish_generation(index_root, staging.path, manifest)
        staging.published = True
    return published.path


def _write_chunks_db(path: Path) -> None:
    """Create a current-schema, empty chunk store."""
    from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

    repo = SqliteChunkRepository(path)
    repo.close()


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
    (rules_dir / "context.md").write_text(
        "refer to docs/mcp-contract.md", encoding="utf-8"
    )

    _publish_generation(tmp_path / "knowcode_index")

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
        "Index generation",
        "Builder drift",
        "Semantic index",
        "Disk footprint",
        "Agent rules",
        "Supported languages",
        "Freshness",
        "Codebase Quality",
        "Native dependencies",
        "Optional dependencies",
    }
    assert all(check["status"] in ("pass", "warn") for check in payload["checks"]), (
        payload
    )


def test_doctor_warns_about_the_pre_generation_flat_layout(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index")

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Index generation")
    assert check.status == "warn"
    assert "flat layout" in check.message


def test_doctor_fails_a_generation_whose_artifact_was_tampered_with(
    tmp_path: Path,
) -> None:
    """The generation check is what proves the digested artifacts still match."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    generation_path = _publish_generation(tmp_path / "knowcode_index")
    (generation_path / "index_manifest.json").write_text("{}", encoding="utf-8")

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Index generation")
    assert check.status == "fail"
    assert "checksum mismatch" in check.message
    assert report.ok is False


def test_doctor_fails_when_no_retained_generation_validates(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    generation_path = _publish_generation(tmp_path / "knowcode_index")
    (generation_path / generations.MANIFEST_FILENAME).unlink()

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Index generation")
    assert check.status == "fail"
    assert "No usable index generation" in check.message


def test_doctor_warns_about_a_graph_only_generation(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _publish_generation(tmp_path / "knowcode_index", kind=generations.KIND_GRAPH_ONLY)

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Index generation")
    assert check.status == "warn"
    assert "graph-only" in check.message


def test_doctor_warns_when_readers_fall_back_past_the_pointer(tmp_path: Path) -> None:
    import shutil

    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    index_root = tmp_path / "knowcode_index"
    _publish_generation(index_root)
    newest = _publish_generation(index_root)
    shutil.rmtree(newest)

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Index generation")
    assert check.status == "warn"
    assert "readers fall back" in check.message


def test_doctor_reads_the_store_from_the_published_generation(tmp_path: Path) -> None:
    """A stale flat knowledge.db must not be preferred over the generation."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    generation_path = _publish_generation(tmp_path / "knowcode_index")
    (tmp_path / "knowledge.db").write_bytes(b"not a database")

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    store_check = next(c for c in report.checks if c.name == "Knowledge store")
    assert store_check.status == "pass"
    assert str(generation_path) in store_check.message


def test_doctor_uses_current_sqlite_store_built_by_cli(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_sqlite_store(tmp_path / "knowledge.db")
    _write_index(tmp_path / "knowcode_index")

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    store_check = next(
        check for check in payload["checks"] if check["name"] == "Knowledge store"
    )
    assert store_check["status"] == "pass"
    assert "SQLite schema v1" in store_check["message"]


def test_doctor_reports_missing_store_and_index(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    failed = {
        check["name"]: check for check in payload["checks"] if check["status"] == "fail"
    }
    assert "Knowledge store" in failed
    assert "Semantic index" in failed
    assert "knowcode build" in failed["Knowledge store"]["hint"]
    assert "knowcode build" in failed["Semantic index"]["hint"]


def _write_legacy_chunks_db(index_dir: Path) -> None:
    """Write a baseline v1 chunks.db without the embedding column."""
    conn = sqlite3.connect(str(index_dir / "chunks.db"))
    conn.execute(
        "CREATE TABLE chunks (chunk_id TEXT UNIQUE, entity_id TEXT, "
        "content TEXT, tokens_text TEXT, metadata_json TEXT, file_path TEXT)"
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, entity_id, content, tokens_text, "
        "metadata_json, file_path) VALUES ('c1', '/x.py::X', 'c', 't', '{}', '/x.py')"
    )
    conn.commit()
    conn.close()


def test_doctor_fails_closed_on_legacy_chunks_db(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index")
    # Overwrite the index dir with a legacy chunks.db that lacks embeddings.
    _write_legacy_chunks_db(tmp_path / "knowcode_index")

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    payload = json.loads(result.output)
    semantic = next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )
    assert semantic["status"] == "fail"
    assert "chunks" in semantic["message"].lower()
    assert "knowcode build" in semantic["hint"]


def test_doctor_passes_with_current_chunks_db(tmp_path: Path) -> None:
    from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    index_dir = tmp_path / "knowcode_index"
    _write_index(index_dir)
    # Replace the legacy chunks.json fixture with a current-schema chunks.db.
    (index_dir / "chunks.json").unlink()
    repo = SqliteChunkRepository(index_dir / "chunks.db")
    repo.close()

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    semantic = next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )
    assert semantic["status"] == "pass"


def test_doctor_missing_optional_dependencies_points_to_ideal_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = []

    monkeypatch.setattr(readiness, "find_spec", lambda _module: None)

    def _missing_import(_module: str):  # type: ignore[no-untyped-def]
        raise ImportError("missing")

    monkeypatch.setattr(doctor_module.importlib, "import_module", _missing_import)

    doctor_module._check_dependencies(checks)

    optional = next(check for check in checks if check.name == "Optional dependencies")
    assert optional.status == "warn"
    assert "knowcode[all,mcp,voyageai]" in optional.hint
    assert optional.hint.count("knowcode[llm]") == 0


def test_doctor_python_runtime_warns_for_unsupported_uvx_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = []
    monkeypatch.setattr(doctor_module.sys, "version_info", (3, 14, 0))

    doctor_module._check_python_runtime(checks)

    runtime = checks[0]
    assert runtime.status == "warn"
    assert "uvx --python 3.12" in runtime.hint
    assert runtime.suggestions[0].command.startswith("uvx --python 3.12")


def test_doctor_accepts_lancedb_vector_artifact(tmp_path: Path) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _write_index(tmp_path / "knowcode_index", backend="lancedb")

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    semantic = next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )
    assert semantic["status"] == "pass"


def test_doctor_fails_closed_on_legacy_lancedb_vector_metadata(tmp_path: Path) -> None:
    """A v1 LanceDB envelope predates the exact-key table (Step 12)."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    index_dir = tmp_path / "knowcode_index"
    _write_index(index_dir, backend="lancedb")
    (index_dir / "vectors.json").write_text(
        json.dumps({"schema_version": 1, "dimension": 1024, "backend": "lancedb"}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    semantic = next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )
    assert semantic["status"] == "fail"
    assert "knowcode build" in semantic["hint"]


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
    semantic = next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )
    assert semantic["status"] == "fail"
    assert "dimension mismatch" in semantic["message"]


def test_doctor_checks_rules_freshness_and_unsupported_extensions(
    tmp_path: Path,
) -> None:
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
    (rules_dir / "context.md").write_text(
        "refer to docs/mcp-contract.md", encoding="utf-8"
    )
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


# ----------------------------------------------------------------------
# Builder drift (BL-32: stale tool environments served false greens)
# ----------------------------------------------------------------------


def test_doctor_fails_when_running_code_differs_from_the_store_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _publish_generation(
        tmp_path / "knowcode_index",
        builder={"code_fingerprint": "sha256:built-elsewhere", "code_files": 3},
    )
    monkeypatch.setattr(
        doctor_module, "package_code_fingerprint", lambda: "sha256:running-here"
    )

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Builder drift")
    assert check.status == "fail"
    assert "sha256:built-elsewhere" in check.message
    assert "sha256:running-here" in check.message
    assert report.ok is False


def test_doctor_passes_when_running_code_matches_the_store_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _publish_generation(
        tmp_path / "knowcode_index",
        builder={"code_fingerprint": "sha256:same", "code_files": 3},
    )
    monkeypatch.setattr(
        doctor_module, "package_code_fingerprint", lambda: "sha256:same"
    )

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Builder drift")
    assert check.status == "pass"


def test_doctor_warns_when_generation_predates_the_builder_stamp(
    tmp_path: Path,
) -> None:
    """A generation built before BL-32 carries no fingerprint; that is a
    warning to rebuild, not corruption."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _publish_generation(tmp_path / "knowcode_index")

    report = doctor_module.run_doctor(store_path=tmp_path, config_path=config)

    check = next(c for c in report.checks if c.name == "Builder drift")
    assert check.status == "warn"
    assert "predates" in check.message


# --- A dummy-built semantic index never passes while a real embedder is
# --- configured, key present or not (BL-35) --------------------------------


def _write_config_without_embedding_models(path: Path) -> None:
    path.write_text(
        """
natural_language_models:
  - name: gemini-test
    provider: google
    api_key_env: KC_TEST_LLM_KEY
config:
  sufficiency_threshold: 0.8
""",
        encoding="utf-8",
    )


def _semantic_check(result: Any) -> dict:
    payload = json.loads(result.output)
    return next(
        check for check in payload["checks"] if check["name"] == "Semantic index"
    )


def test_doctor_fails_a_dummy_built_index_when_no_key_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-35: without a key both sides used to degrade to dummy and agree."""
    monkeypatch.delenv("KC_TEST_EMBED_KEY", raising=False)
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    semantic = _semantic_check(result)
    assert semantic["status"] == "fail"
    assert "dummy" in semantic["message"]
    assert "voyage-code-3" in semantic["message"]


def test_doctor_fails_a_dummy_built_index_when_the_key_is_present(
    tmp_path: Path,
) -> None:
    """The half BL-27 already caught stays caught."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert _semantic_check(result)["status"] == "fail"


def test_doctor_passes_a_dummy_index_when_no_embedding_model_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing better was ever asked for, so the dummy is the intent."""
    monkeypatch.delenv("KC_TEST_EMBED_KEY", raising=False)
    config = tmp_path / "aimodels.yaml"
    _write_config_without_embedding_models(config)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert _semantic_check(result)["status"] == "pass"


def test_doctor_fails_a_real_index_the_running_process_cannot_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real index plus a missing key means queries would use the dummy."""
    monkeypatch.delenv("KC_TEST_EMBED_KEY", raising=False)
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(tmp_path / "knowcode_index")

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert _semantic_check(result)["status"] == "fail"


def test_doctor_names_the_key_before_the_rebuild_when_none_is_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuilding without a key selects the dummy again, so say the key first."""
    monkeypatch.delenv("KC_TEST_EMBED_KEY", raising=False)
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    assert "KC_TEST_EMBED_KEY" in _semantic_check(result)["hint"]


def test_doctor_keeps_the_rebuild_hint_when_a_key_is_usable(tmp_path: Path) -> None:
    """With a key present a rebuild is the whole fix, so nothing else is said."""
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )

    hint = _semantic_check(result)["hint"]
    assert "KC_TEST_EMBED_KEY" not in hint
    assert "knowcode build" in hint


# --- An embedding entry this build cannot construct is reported by the Config
# --- check, and does not take the command down (BL-36) ---------------------


def _write_config_with_unsupported_embedder(path: Path, *, with_voyage: bool) -> None:
    """Configure `provider: local`, which `build_provider_from_model` refuses."""
    voyage = (
        """
  - name: voyage-code-3
    provider: voyageai
    api_key_env: KC_TEST_EMBED_KEY
"""
        if with_voyage
        else ""
    )
    path.write_text(
        f"""
natural_language_models:
  - name: gemini-test
    provider: google
    api_key_env: KC_TEST_LLM_KEY
embedding_models:
  - name: bge-m3
    provider: local
    api_key_env: KC_LOCAL_KEY{voyage}
config:
  sufficiency_threshold: 0.8
""",
        encoding="utf-8",
    )


def _config_check(result: Any) -> dict:
    payload = json.loads(result.output)
    return next(check for check in payload["checks"] if check["name"] == "Config")


def _run_doctor(tmp_path: Path, config: Path) -> Any:
    return CliRunner().invoke(
        cli_module.cli,
        ["doctor", "--store", str(tmp_path), "--config", str(config), "--json"],
    )


def test_doctor_completes_every_check_past_an_unbuildable_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-36: `NotImplementedError` escaped `_check_semantic_index`.

    The blast radius is wider than one check. `_check_semantic_index` is
    called unguarded, so the traceback also cost the four checks sequenced
    after it -- a diagnostic silently reporting less than it ran.
    """
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")
    config = tmp_path / "aimodels.yaml"
    _write_config_with_unsupported_embedder(config, with_voyage=False)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    result = _run_doctor(tmp_path, config)

    assert not isinstance(result.exception, NotImplementedError), result.exception
    names = [check["name"] for check in json.loads(result.output)["checks"]]
    assert "Semantic index" in names
    for trailing in (
        "Disk footprint",
        "Agent rules",
        "Supported languages",
        "Freshness",
    ):
        assert trailing in names, names


def test_doctor_fails_config_naming_the_only_embedder_it_cannot_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no buildable entry left, nothing configured can ever embed.

    The BL-35 comparison cannot catch this one: `configured_embedding_config`
    steps over the unusable entry too, so both yardsticks read `dummy` and
    agree. Naming the entry is the only place the truth survives.
    """
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")
    config = tmp_path / "aimodels.yaml"
    _write_config_with_unsupported_embedder(config, with_voyage=False)
    _write_store(tmp_path)
    _publish_generation(
        tmp_path / "knowcode_index",
        provider="dummy",
        model_name="deterministic-sha256",
    )

    check = _config_check(_run_doctor(tmp_path, config))

    assert check["status"] == "fail"
    assert "bge-m3" in check["message"]
    assert "local" in check["message"]


def test_doctor_warns_when_an_unbuildable_entry_leaves_a_usable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead entry beside a working one is a config defect, not unreadiness.

    The semantic index check must keep going and settle on the entry that
    does build, which is the half of BL-36 the crash hid.
    """
    monkeypatch.setenv("KC_LOCAL_KEY", "test-key")
    monkeypatch.setenv("KC_TEST_EMBED_KEY", "test-key")
    config = tmp_path / "aimodels.yaml"
    _write_config_with_unsupported_embedder(config, with_voyage=True)
    _write_store(tmp_path)
    _publish_generation(tmp_path / "knowcode_index")

    result = _run_doctor(tmp_path, config)

    check = _config_check(result)
    assert check["status"] == "warn"
    assert "bge-m3" in check["message"]
    assert _semantic_check(result)["status"] == "pass"


def test_doctor_config_passes_when_every_embedder_is_buildable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report stays quiet about a config with nothing wrong with it."""
    monkeypatch.setenv("KC_TEST_EMBED_KEY", "test-key")
    config = tmp_path / "aimodels.yaml"
    _write_config(config)
    _write_store(tmp_path)
    _publish_generation(tmp_path / "knowcode_index")

    check = _config_check(_run_doctor(tmp_path, config))

    assert check["status"] == "pass"
    assert "bge-m3" not in check["message"]
