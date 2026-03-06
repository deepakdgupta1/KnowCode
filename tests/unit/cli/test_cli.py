"""Unit tests for CLI commands."""

import importlib
import json

from click.testing import CliRunner
import pytest

cli_module = importlib.import_module("knowcode.cli.cli")


def test_cli_analyze_query_stats_context(tmp_path) -> None:  # type: ignore
    """Basic CLI commands should run against a temporary project."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    runner = CliRunner()
    analyze = runner.invoke(cli_module.cli, ["analyze", str(tmp_path), "--output", str(tmp_path)])
    assert analyze.exit_code == 0

    store_path = tmp_path / "knowcode_knowledge.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    entity_id = next(iter(data["entities"].keys()))

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
