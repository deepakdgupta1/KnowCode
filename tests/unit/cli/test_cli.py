"""Unit tests for CLI commands."""

import json

from click.testing import CliRunner

from knowcode.cli import cli


def test_cli_analyze_query_stats_context(tmp_path):
    """Basic CLI commands should run against a temporary project."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    runner = CliRunner()
    analyze = runner.invoke(cli, ["analyze", str(tmp_path), "--output", str(tmp_path)])
    assert analyze.exit_code == 0

    store_path = tmp_path / "knowcode_knowledge.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    entity_id = next(iter(data["entities"].keys()))

    query = runner.invoke(cli, ["query", "search", "foo", "--store", str(tmp_path)])
    assert query.exit_code == 0
    assert "foo" in query.output

    stats = runner.invoke(cli, ["stats", "--store", str(tmp_path)])
    assert stats.exit_code == 0
    assert "Total Entities" in stats.output

    context = runner.invoke(cli, ["context", entity_id, "--store", str(tmp_path), "--max-tokens", "200"])
    assert context.exit_code == 0
