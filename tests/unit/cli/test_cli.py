"""Unit tests for CLI commands."""

import importlib
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

cli_module = importlib.import_module("knowcode.cli.cli")


def _published_store(root: Path) -> Path:
    """Resolve the knowledge store of the published generation (Step 14).

    ``knowledge.db`` no longer sits beside the sources: it is one of the four
    artifacts published together inside ``knowcode_index/generations/<id>/``.
    """
    from knowcode.indexing.generations import resolve_current_generation

    generation = resolve_current_generation(root / "knowcode_index")
    assert generation is not None, "build published no generation"
    return generation.knowledge_db


def test_cli_analyze_query_stats_context(tmp_path) -> None:  # type: ignore
    """Basic CLI commands should run against a temporary project."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    runner = CliRunner()
    analyze = runner.invoke(cli_module.cli, ["analyze", str(tmp_path), "--output", str(tmp_path)])
    assert analyze.exit_code == 0

    store_path = _published_store(tmp_path)
    import sqlite3
    conn = sqlite3.connect(store_path)
    entity_id = conn.execute("SELECT entity_id FROM entities LIMIT 1").fetchone()[0]
    conn.close()

    query = runner.invoke(cli_module.cli, ["query", "search", "foo", "--store", str(tmp_path)])
    assert query.exit_code == 0
    assert "foo" in query.output

    stats = runner.invoke(cli_module.cli, ["stats", "--store", str(tmp_path)])
    assert stats.exit_code == 0
    assert "Total Entities" in stats.output

    context = runner.invoke(
        cli_module.cli, ["context", entity_id, "--store", str(tmp_path), "--max-tokens", "200"]
    )
    assert context.exit_code == 0

    docs_dir = tmp_path / "docs"
    export = runner.invoke(
        cli_module.cli,
        ["export", "--store", str(tmp_path), "--output", str(docs_dir)],
    )
    assert export.exit_code == 0, export.output
    assert "Documents:" in export.output
    assert (docs_dir / "index.md").exists()
    assert (docs_dir / "architecture.md").exists()
    manifest = json.loads(
        (docs_dir / "documentation_manifest.json").read_text(encoding="utf-8")
    )
    assert "index.md" in manifest["documents"]
    assert "architecture.md" in manifest["documents"]


def test_cli_build_defaults_to_current_directory(tmp_path, monkeypatch) -> None:  # type: ignore
    """`knowcode build` with no arguments builds the store in the cwd."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["build"])

    assert result.exit_code == 0, result.output
    assert "Build complete" in result.output
    assert "Generation:" in result.output
    store_path = _published_store(tmp_path)
    assert store_path.exists()
    import sqlite3
    conn = sqlite3.connect(store_path)
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count > 0
    conn.close()


def test_cli_install_dry_run_prints_ideal_dependency_set() -> None:
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "knowcode[all,mcp,voyageai]" in result.output
    assert "MCP server" in result.output
    assert "VoyageAI embeddings and reranking" in result.output
    assert "Dry run only" in result.output


def test_cli_install_runs_pip_for_ideal_dependency_set(monkeypatch) -> None:  # type: ignore
    runner = CliRunner()
    calls = []

    def _run(command, check):  # type: ignore
        calls.append((command, check))

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(cli_module.cli, ["install", "--upgrade", "--user"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            [
                cli_module.sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--user",
                "knowcode[all,mcp,voyageai]",
            ],
            True,
        )
    ]
    assert "KnowCode ideal setup dependencies installed" in result.output


@pytest.mark.parametrize(
    ("args", "expected_message"),
    [
        (["index", "."], "Install knowcode[search] to use 'knowcode index'."),
        (
            ["semantic-search", "hello", "--index", ".", "--store", "."],
            "Install knowcode[search] to use 'knowcode semantic-search'.",
        ),
        (["server"], "Install knowcode[server] to use 'knowcode server'."),
        (["ask", "hello"], "Install knowcode[llm] to use 'knowcode ask'."),
    ],
)
def test_optional_dependency_guards(monkeypatch, args: list[str], expected_message: str) -> None:  # type: ignore
    runner = CliRunner()

    def _raise(extra: str, command: str, modules):  # type: ignore
        raise ImportError(f"Install knowcode[{extra}] to use '{command}'.")

    monkeypatch.setattr(cli_module, "require_extra", _raise)
    result = runner.invoke(cli_module.cli, args)
    assert result.exit_code == 1
    assert expected_message in result.output


def test_server_watch_dependency_guard(monkeypatch) -> None:  # type: ignore
    runner = CliRunner()

    def _raise_on_watch(extra: str, command: str, modules):  # type: ignore
        if extra == "watch":
            raise ImportError(f"Install knowcode[{extra}] to use '{command}'.")
        return None

    monkeypatch.setattr(cli_module, "require_extra", _raise_on_watch)
    result = runner.invoke(cli_module.cli, ["server", "--watch"])
    assert result.exit_code == 1
    assert "Install knowcode[watch] to use 'knowcode server --watch'." in result.output


def _telemetry_event(store: Path) -> None:
    """Emit one counted query event the way production does."""
    from knowcode import telemetry

    with telemetry.query_scope(store, query="who calls place_order?") as scope:
        scope.record_retrieval(sufficiency_score=0.9, local_or_escalated="local")
    telemetry.shutdown_telemetry(timeout=5.0)


def test_cli_telemetry_show_reports_aggregates_only(tmp_path) -> None:  # type: ignore
    """`knowcode telemetry show` is the documented inspection surface."""
    _telemetry_event(tmp_path)

    result = CliRunner().invoke(
        cli_module.cli, ["telemetry", "show", "--store", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Total queries: 1" in result.output
    assert "query: 1" in result.output
    assert "place_order" not in result.output


def test_cli_telemetry_clear_deletes_every_file(tmp_path) -> None:  # type: ignore
    """`knowcode telemetry clear` is the documented deletion path (Step 20)."""
    from knowcode import telemetry_files

    _telemetry_event(tmp_path)
    assert telemetry_files.existing_files(tmp_path)

    result = CliRunner().invoke(
        cli_module.cli, ["telemetry", "clear", "--store", str(tmp_path), "--yes"]
    )

    assert result.exit_code == 0
    assert telemetry_files.existing_files(tmp_path) == []


def test_cli_telemetry_clear_asks_before_deleting(tmp_path) -> None:  # type: ignore
    """Declining the prompt leaves the files in place."""
    from knowcode import telemetry_files

    _telemetry_event(tmp_path)

    result = CliRunner().invoke(
        cli_module.cli, ["telemetry", "clear", "--store", str(tmp_path)], input="n\n"
    )

    assert result.exit_code == 1
    assert telemetry_files.existing_files(tmp_path)
