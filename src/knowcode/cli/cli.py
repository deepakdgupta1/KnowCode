"""CLI interface for KnowCode."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import click

from knowcode import __version__
from knowcode.analysis.documentation_synthesizer import DocumentationSynthesizer
from knowcode.errors import KnowCodePrerequisiteError
from knowcode.readiness import (
    IDEAL_SETUP_FEATURES,
    build_install_command,
)
from knowcode.service import KnowCodeService
from knowcode.utils.dependency_guard import require_extra


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """KnowCode - Transform your codebase into an effective knowledge base."""
    pass


def _report_generation(stats: dict[str, Any]) -> None:
    """Report where a build published its generation, or why it did not.

    Step 14 made publication all-or-nothing: the knowledge graph, chunks,
    vectors, and manifest move together. A build that published nothing is an
    error, not a warning with a partially usable result, so it exits non-zero.
    """
    if stats.get("store_path"):
        click.echo(f"\n  Knowledge store: {stats['store_path']}")
    if stats.get("index_path"):
        click.echo(f"  Semantic index: {stats['index_path']}")
    if stats.get("generation_id"):
        click.echo(f"  Generation: {stats['generation_id']}")

    if not stats.get("published", True):
        click.echo(
            "\n  Nothing was published: the previous index generation is still "
            "the current one.",
            err=True,
        )
        click.echo(
            f"    Failed at stage {stats.get('index_error_stage')!r}: "
            f"{stats.get('index_error')}",
            err=True,
        )
        sys.exit(1)

    if stats.get("index_error"):
        click.echo(
            "\n  Note: this generation has no semantic index "
            "(the knowledge graph was still published).",
            err=True,
        )
        click.echo(f"    {stats['index_error']}", err=True)
        click.echo(
            "    Build it later with `knowcode index` once embeddings are configured.",
            err=True,
        )


@cli.command("install")
@click.option(
    "--upgrade",
    is_flag=True,
    help="Upgrade packages while installing the ideal dependency set.",
)
@click.option(
    "--user",
    "user_install",
    is_flag=True,
    help="Install into the Python user site-packages directory.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the installer command without running it.",
)
def install_command(upgrade: bool, user_install: bool, dry_run: bool) -> None:
    """Install dependencies for a full-featured KnowCode setup."""
    command = build_install_command(upgrade=upgrade, user_install=user_install)

    click.echo("Installing KnowCode ideal setup dependencies.")
    click.echo(f"  Includes: {', '.join(IDEAL_SETUP_FEATURES)}")
    click.echo(f"  Command: {shlex.join(command)}")

    if dry_run:
        click.echo("Dry run only; no packages installed.")
        return

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"Dependency installation failed with exit code {exc.returncode}."
        ) from exc

    click.echo("✓ KnowCode ideal setup dependencies installed.")


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=".",
    help="Output directory for knowledge store (default: current directory)",
)
@click.option(
    "--ignore",
    "-i",
    multiple=True,
    help="Additional patterns to ignore",
)
@click.option(
    "--temporal/--no-temporal",
    default=False,
    help="Analyze git history and add temporal context.",
)
@click.option(
    "--coverage",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to Cobertura XML coverage report.",
)
def analyze(
    directory: str,
    output: str,
    ignore: tuple[str, ...],
    temporal: bool,
    coverage: Optional[str],
) -> None:
    """Scan and analyze a codebase.

    DIRECTORY: Path to the codebase to analyze.
    """
    click.echo(f"Analyzing: {directory}")
    click.echo(f"Temporal analysis: {'Enabled' if temporal else 'Disabled'}")
    if coverage:
        click.echo(f"Coverage report: {coverage}")

    service = KnowCodeService()
    stats = service.analyze(
        directory=directory,
        output=output,
        ignore=list(ignore),
        temporal=temporal,
        coverage=coverage,
    )

    click.echo("\n✓ Analysis complete!")
    click.echo(f"  Entities: {stats['total_entities']}")
    click.echo(f"  Relationships: {stats['total_relationships']}")
    if stats.get("total_errors", 0) > 0:
        click.echo(f"  Errors: {stats['total_errors']}")
    if stats.get("indexed_chunks") is not None:
        click.echo(f"  Indexed chunks: {stats['indexed_chunks']}")

    _report_generation(stats)


@cli.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    required=False,
)
@click.option(
    "--ignore",
    "-i",
    multiple=True,
    help="Additional patterns to ignore",
)
@click.option(
    "--temporal/--no-temporal",
    default=False,
    help="Analyze git history and add temporal context.",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for embedding models.",
)
@click.option(
    "--incremental",
    is_flag=True,
    help="Use incremental indexing to speed up subsequent builds.",
)
def build(
    directory: str,
    ignore: tuple[str, ...],
    temporal: bool,
    config: Optional[str],
    incremental: bool,
) -> None:
    """Build the knowledge base and semantic index for a directory.

    Scans DIRECTORY (defaults to the current directory), builds the
    knowledge store, and builds the semantic index alongside it. Run it
    from inside any project with no arguments:

    \b
        knowcode build
    """
    target = Path(directory).resolve()
    click.echo(f"Building KnowCode knowledge base for: {target}")
    if temporal:
        click.echo("Temporal analysis: Enabled")

    from knowcode.config import AppConfig

    app_config = AppConfig.load(config)
    service = KnowCodeService(app_config=app_config)
    stats = service.analyze(
        directory=str(target),
        output=str(target),
        ignore=list(ignore),
        temporal=temporal,
        incremental=incremental,
    )

    click.echo("\n✓ Build complete!")
    click.echo(f"  Entities: {stats['total_entities']}")
    click.echo(f"  Relationships: {stats['total_relationships']}")
    if stats.get("total_errors", 0) > 0:
        click.echo(f"  Errors: {stats['total_errors']}")
    if stats.get("indexed_chunks") is not None:
        click.echo(f"  Indexed chunks: {stats['indexed_chunks']}")

    _report_generation(stats)

    # Show pre-flight quality report if available
    if "preflight_report" in stats:
        _display_preflight_report(stats["preflight_report"])


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="knowcode_index",
    help="Output directory for index (default: knowcode_index)",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for embedding models.",
)
@click.option(
    "--incremental",
    is_flag=True,
    help="Use incremental indexing to speed up subsequent builds.",
)
def index(
    directory: str, output: str, config: Optional[str], incremental: bool
) -> None:
    """Build semantic search index for a codebase.

    DIRECTORY: Path to the codebase to index.
    """
    click.echo(f"Indexing: {directory}")

    try:
        from knowcode.config import AppConfig

        app_config = AppConfig.load(config)
        if app_config.vector_backend == "lancedb":
            require_extra("search", "knowcode index", ("lancedb",))

        index_path = Path(output)
        # A semantic index is one artifact class of a generation, never a
        # standalone directory: publishing it alone would leave chunks and
        # vectors that no reader resolves and that match no knowledge graph.
        service = KnowCodeService(store_path=directory, app_config=app_config)
        result = service.build_generation(
            directory, index_path, incremental=incremental
        )
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not result.published:
        click.echo(
            f"Error: nothing was published (stage {result.stage!r}): {result.error}",
            err=True,
        )
        click.echo(
            "  The previous index generation, if any, is still the current one.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"✓ Indexing complete! Created {result.chunk_count} chunks.")
    click.echo(f"  Saved to: {output}")
    click.echo(f"  Generation: {result.generation_id}")
    if result.error:
        click.echo(f"  Note: {result.error}", err=True)


@cli.command()
@click.argument(
    "query_type", type=click.Choice(["callers", "callees", "deps", "search"])
)
@click.argument("target")
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON",
)
def query(query_type: str, target: str, store: str, as_json: bool) -> None:
    """Query the knowledge store.

    QUERY_TYPE: Type of query (callers, callees, deps, search)
    TARGET: Entity ID or search pattern
    """
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo(
            "Error: Knowledge store not found. Run 'knowcode build' first.", err=True
        )
        sys.exit(1)

    results: list[dict[str, Any]] = []

    if query_type == "search":
        results = service.search(target)

    elif query_type == "callers":
        results = service.get_callers(target)

    elif query_type == "callees":
        results = service.get_callees(target)

    elif query_type == "deps":
        entity = service.store.get_entity(target) or next(
            iter(service.store.search(target)), None
        )
        if entity:
            deps = service.store.get_dependencies(entity.id)
            for d in deps:
                results.append(
                    {
                        "id": d.id,
                        "kind": d.kind.value,
                        "name": d.qualified_name,
                    }
                )

    # Output results
    if as_json:
        click.echo(json.dumps(results, indent=2))
    else:
        if not results:
            click.echo("No results found.")
        else:
            for r in results:
                name = r.get("name", r.get("id", "unknown"))
                if "qualified_name" in r:
                    name = r["qualified_name"]
                extra = ""
                if "file" in r:
                    extra = f" ({r['file']}:{r.get('line', '')})"
                elif "kind" in r:
                    extra = f" [{r['kind']}]"
                click.echo(f"  • {name}{extra}")


@cli.command("semantic-search")
@click.argument("query_text", nargs=-1, required=True)
@click.option(
    "--index",
    "-i",
    type=click.Path(exists=True, file_okay=False),
    default="knowcode_index",
    help="Path to index directory (default: knowcode_index)",
)
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store (directory or file)",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for embedding/reranking models.",
)
@click.option(
    "--limit", "-l", type=int, default=5, help="Number of results (default: 5)"
)
def semantic_search(
    query_text: tuple[str],
    index: str,
    store: str,
    config: Optional[str],
    limit: int,
) -> None:
    """Search codebase using semantic similarity.

    QUERY_TEXT: The search query.
    """
    question = " ".join(query_text)
    click.echo(f"Searching for: '{question}'...")

    try:
        from knowcode.config import AppConfig

        app_config = AppConfig.load(config)
        if app_config.vector_backend == "lancedb":
            require_extra("search", "knowcode semantic-search", ("lancedb",))
        service = KnowCodeService(store_path=store, app_config=app_config)
        engine = service.get_search_engine(index_path=index)

        results = engine.search(question, limit=limit)

        if not results:
            click.echo("No relevant code found.")
        else:
            for i, chunk in enumerate(results):
                click.echo(f"\n[{i + 1}] {chunk.entity_id}")
                content = chunk.content
                if len(content) > 300:
                    content = content[:300] + "..."
                click.echo("-" * 40)
                click.echo(content)
                click.echo("-" * 40)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("target")
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--max-tokens",
    "-m",
    type=int,
    default=2000,
    help="Maximum tokens in context (default: 2000)",
)
def context(target: str, store: str, max_tokens: int) -> None:
    """Generate context bundle for an entity.

    TARGET: Entity ID or search pattern
    """
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo(
            "Error: Knowledge store not found. Run 'knowcode build' first.", err=True
        )
        sys.exit(1)

    try:
        bundle_dict = service.get_context(target, max_tokens=max_tokens)
        click.echo(bundle_dict["context_text"])
        click.echo(
            f"\n--- {len(bundle_dict['context_text'])} chars, {bundle_dict['total_tokens']} tokens, {len(bundle_dict['included_entities'])} entities ---",
            err=True,
        )
        if bundle_dict["truncated"]:
            click.echo("(truncated)", err=True)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="docs",
    help="Output directory for documentation",
)
def export(store: str, output: str) -> None:
    """Export knowledge store as multi-level Markdown documentation."""
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo(
            "Error: Knowledge store not found. Run 'knowcode build' first.", err=True
        )
        sys.exit(1)

    output_dir = Path(output)
    bundle = DocumentationSynthesizer(service.store).write(output_dir)
    index_path = output_dir / "index.md"
    manifest_path = output_dir / DocumentationSynthesizer.MANIFEST_FILENAME

    click.echo(f"✓ Exported documentation to: {output_dir}")
    click.echo(f"  Documents: {len(bundle.documents)}")
    click.echo(f"  Index: {index_path}")
    click.echo(f"  Manifest: {manifest_path}")


@cli.command()
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
def stats(store: str) -> None:
    """Show statistics about the knowledge store."""
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo(
            "Error: Knowledge store not found. Run 'knowcode build' first.", err=True
        )
        sys.exit(1)

    s = service.get_stats()
    click.echo("Knowledge Store Statistics")
    click.echo("-" * 30)

    click.echo(f"\nTotal Entities: {s['total_entities']}")
    for kind, count in sorted(s["entities_by_kind"].items()):
        click.echo(f"  {kind}: {count}")

    click.echo(f"\nTotal Relationships: {s['total_relationships']}")
    for kind, count in sorted(s["relationships_by_type"].items()):
        click.echo(f"  {kind}: {count}")


@cli.group()
def telemetry() -> None:
    """Inspect or delete the local telemetry log.

    Telemetry never leaves this machine and never contains your questions or
    your code — see docs/user/telemetry.md for the exact schema.
    """


@telemetry.command("show")
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to the store directory whose telemetry should be summarized",
)
def telemetry_show(store: str) -> None:
    """Summarize local telemetry."""
    from knowcode import telemetry_files
    from knowcode.telemetry import get_telemetry_summary, raw_capture_enabled

    summary = get_telemetry_summary(store)
    click.echo("Telemetry Summary")
    click.echo("-" * 30)
    click.echo(f"  Schema version: {summary['schema_version']}")
    click.echo(f"  Total queries: {summary['total_queries']}")
    click.echo(f"  Local routing rate: {summary['local_routing_rate']:.0%}")
    click.echo(f"  Average sufficiency: {summary['average_sufficiency_score']:.2f}")
    click.echo(f"  User-marked misses: {summary['user_marked_misses']}")

    if summary["events_by_type"]:
        click.echo("\n  Events by type:")
        for event_type, count in sorted(summary["events_by_type"].items()):
            click.echo(f"    {event_type}: {count}")

    files = telemetry_files.existing_files(store)
    click.echo(f"\n  Files ({len(files)}):")
    for path in files:
        click.echo(f"    {path}")
    if raw_capture_enabled():
        click.echo(
            f"\n  WARNING: {telemetry_files.RAW_TELEMETRY_FILENAME} is enabled; "
            "raw query text is being stored.",
        )


@telemetry.command("clear")
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to the store directory whose telemetry should be deleted",
)
@click.option("--yes", is_flag=True, help="Delete without confirmation")
def telemetry_clear(store: str, yes: bool) -> None:
    """Delete every local telemetry file, including the correlation key."""
    from knowcode import telemetry_files
    from knowcode.telemetry import delete_telemetry

    files = telemetry_files.existing_files(store)
    if not files:
        click.echo("No telemetry files to delete.")
        return
    if not yes:
        click.echo("The following files will be deleted:")
        for path in files:
            click.echo(f"  {path}")
        click.confirm("Delete them?", abort=True)

    result = delete_telemetry(store)
    click.echo(f"Deleted {result['removed']} telemetry file(s).")


@cli.command()
@click.option(
    "--store",
    "-s",
    type=click.Path(),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--index",
    "-i",
    "index_path",
    type=click.Path(),
    help="Path to semantic index directory (default: beside the store)",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(),
    help="Path to configuration file (aimodels.yaml)",
)
@click.option(
    "--max-disk-mb",
    type=float,
    default=500.0,
    show_default=True,
    help="Warn if KnowCode artifacts exceed this size.",
)
@click.option(
    "--mcp/--no-mcp",
    default=False,
    help="Spawn the MCP server and verify list_tools plus one tool call.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output the doctor report as JSON.",
)
def doctor(
    store: str,
    index_path: Optional[str],
    config: Optional[str],
    max_disk_mb: float,
    mcp: bool,
    as_json: bool,
) -> None:
    """Check whether the local KnowCode setup is ready."""
    from knowcode.doctor import run_doctor

    report = run_doctor(
        store_path=store,
        index_path=index_path,
        config_path=config,
        max_disk_mb=max_disk_mb,
        include_mcp=mcp,
    )

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        click.echo("KnowCode Doctor")
        click.echo("-" * 30)
        for check in report.checks:
            click.echo(f"[{check.status.upper()}] {check.name}: {check.message}")
            if check.hint:
                click.echo(f"       Hint: {check.hint}")

    if not report.ok:
        sys.exit(1)


def _display_preflight_report(report: dict[str, Any]) -> None:
    """Display a pre-flight quality report card in the terminal."""
    overall = report.get("overall_score", 0.0)
    grade = report.get("overall_grade", "?")
    click.echo(f"\n  Pre-flight Quality Assessment: {grade} ({overall:.0%})")
    click.echo("  " + "─" * 56)

    dimensions = report.get("dimensions", [])
    for dim in dimensions:
        name = dim.get("dimension", "").replace("_", " ").title()
        score = dim.get("score", 0.0)
        dim_grade = dim.get("grade", "?")
        detail = dim.get("detail", "")
        # Grade-coloured indicator
        indicator = "●" if score >= 0.75 else "◐" if score >= 0.5 else "○"
        click.echo(f"  {indicator} [{dim_grade}] {name:.<35s} {score:.0%}")
        if detail:
            click.echo(f"        {detail}")

    recommendations = report.get("recommendations", [])
    if recommendations:
        click.echo(f"\n  Recommendations ({len(recommendations)}):")
        for i, rec in enumerate(recommendations, 1):
            click.echo(f"    {i}. {rec}")

    summary = report.get("summary", "")
    if summary:
        click.echo(f"\n  {summary}")


@cli.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    required=False,
)
@click.option(
    "--ignore",
    "-i",
    multiple=True,
    help="Additional patterns to ignore",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for custom weights.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output the report as JSON.",
)
def preflight(
    directory: str,
    ignore: tuple[str, ...],
    config: Optional[str],
    as_json: bool,
) -> None:
    """Run a pre-flight quality assessment on a codebase.

    Scans and parses DIRECTORY (defaults to current directory) to evaluate
    how well KnowCode's surfaces will perform on this codebase. Produces a
    report card with grades across 10 quality dimensions.

    \b
    This is a lightweight check that does NOT build a knowledge store or
    semantic index — it only scans and parses.
    """
    target = Path(directory).resolve()
    click.echo(f"Running pre-flight assessment on: {target}")

    from knowcode.config import AppConfig

    app_config = AppConfig.load(config)
    service = KnowCodeService(app_config=app_config)

    try:
        report = service.preflight(
            directory=str(target),
            ignore=list(ignore),
        )
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        _display_preflight_report(report)

    # Warn if below configured threshold
    min_score = app_config.preflight.min_score
    if min_score > 0.0 and report.get("overall_score", 0.0) < min_score:
        click.echo(
            f"\n  ⚠  Quality score ({report['overall_score']:.0%}) is below "
            f"the configured threshold ({min_score:.0%}).",
            err=True,
        )
        sys.exit(1)


@cli.command()
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind the server to (default: 127.0.0.1)",
)
@click.option(
    "--port",
    default=8000,
    help="Port to bind the server to (default: 8000)",
)
@click.option(
    "--watch",
    is_flag=True,
    help="Watch for file changes and re-index automatically",
)
def server(store: str, host: str, port: int, watch: bool) -> None:
    """Start the KnowCode intelligence server."""
    try:
        require_extra("server", "knowcode server", ("fastapi", "uvicorn", "slowapi"))
        if watch:
            require_extra("watch", "knowcode server --watch", ("watchdog",))
        from knowcode.api.main import start_server

        click.echo(f"Starting KnowCode server on {host}:{port}")
        click.echo(f"Using knowledge store: {store}")
        if watch:
            click.echo("Watch mode enabled.")

        start_server(host=host, port=port, store_path=store, watch=watch)
    except ImportError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("target", required=False)
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--limit",
    "-l",
    type=int,
    default=10,
    help="Limit number of revisions",
)
def history(target: Optional[str], store: str, limit: int) -> None:
    """Show history of the codebase or a specific entity.

    TARGET: Optional entity ID or search pattern. If omitted, shows commit log.
    """
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo(
            "Error: Knowledge store not found. Run 'knowcode build' first.", err=True
        )
        sys.exit(1)

    try:
        result = service.get_history(target=target, limit=limit)
    except ValueError as e:
        click.echo(str(e))
        return

    entries = result["entries"]

    if result["scope"] == "commits":
        click.echo(f"Recent History (showing {len(entries)} of {result['total']}):")
        for entry in entries:
            click.echo(
                f"[{entry['date'] or 'Unknown date'}] {entry['commit']} - {entry['author']}"
            )
            click.echo(f"  {entry['summary']}")
        return

    entity = result["entity"]
    click.echo(f"History for {entity['qualified_name']} ({entity['kind']}):")

    if not entries:
        click.echo("  No recorded history (scan with --temporal).")
        return

    for entry in entries:
        stats = f"(+{entry['insertions']}/-{entry['deletions']})"
        click.echo(
            f"  {entry['date'] or ''} {entry['commit']} {stats}: {entry['summary']}"
        )


@cli.command()
@click.argument("query_text", nargs=-1, required=True)
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file for model priorities",
)
def ask(query_text: tuple[str], store: str, config: Optional[str]) -> None:
    """Ask a question about the codebase using AI.

    QUERY_TEXT: The question to ask.
    """
    question = " ".join(query_text)

    try:
        require_extra(
            "llm", "knowcode ask", ("openai", "google.genai", "google.api_core")
        )
        from knowcode.llm.agent import Agent
        from knowcode.config import AppConfig

        app_config = AppConfig.load(config)
        service = KnowCodeService(store_path=store, app_config=app_config)
        agent = Agent(service, config=app_config)

        click.echo(f"🤔 Asking KnowCode: '{question}'...")
        answer = agent.answer(question)
        click.echo("\n" + answer)
    except KnowCodePrerequisiteError as e:
        click.echo(f"Error: {e}", err=True)
        click.echo(f"Hint: {e.hint}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command("mcp-server")
@click.option(
    "--store",
    "-s",
    type=click.Path(exists=True),
    default=None,
    help=(
        "Repository root. Defaults to $CLAUDE_PROJECT_DIR, then the working "
        "directory, so one registration serves every repository."
    ),
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file for model priorities",
)
@click.option(
    "--legacy-tools/--no-legacy-tools",
    default=False,
    help=(
        "Also advertise the five deprecated flat tools (search_codebase, "
        "get_entity_context, trace_calls, retrieve_context_for_query, "
        "assess_codebase_quality). Off by default."
    ),
)
def mcp_server(store: Optional[str], config: Optional[str], legacy_tools: bool) -> None:
    """Start MCP server for IDE integration.

    Exposes KnowCode over the Model Context Protocol (STDIO transport) as
    three consolidated tools, each selecting a capability via an ``action``:

    \b
    - knowcode_retrieve:  query | search | context | trace | semantic_search
    - knowcode_lifecycle: build | index | export
    - knowcode_inspect:   job_status | doctor | freshness | quality | stats |
                          preflight | history | telemetry

    The server starts whether or not a knowledge store exists: an agent in a
    repository KnowCode has never seen calls knowcode_lifecycle
    action='build' to create one, so a missing store is reported per-action
    rather than preventing startup.

    Example client configuration:

    \b
        {
            "knowcode": {
                "command": "knowcode",
                "args": ["mcp-server"]
            }
        }
    """
    from knowcode.mcp.roots import resolve_server_root, store_is_ready

    try:
        store_path = resolve_server_root(explicit=store)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Shared with the server's per-action gate: a current build publishes the
    # graph inside a generation, so checking only the legacy JSON would report
    # "not built" for a repository that is in fact fully searchable.
    store_ready = store_is_ready(store_path)

    try:
        from knowcode.mcp.server import run_server

        click.echo("🔌 Starting MCP server...", err=True)
        click.echo(f"   Root: {store_path}", err=True)
        click.echo("   Transport: STDIO", err=True)
        click.echo(
            "   Tools: knowcode_retrieve, knowcode_lifecycle, knowcode_inspect"
            + (" (+5 legacy)" if legacy_tools else ""),
            err=True,
        )
        if store_ready:
            click.echo("   Store: ready", err=True)
            # BL-32: a uvx path-source install keeps serving the tool
            # environment it was built with; say so when the store disagrees.
            from knowcode.indexing.generations import current_builder_drift

            drift = current_builder_drift(store_path / "knowcode_index")
            if drift:
                click.echo(f"   WARNING: {drift}", err=True)
        else:
            # Not an error: this is the bootstrap path the build action exists
            # to serve. Retrieval actions report it with an actionable hint.
            click.echo(
                "   Store: not built yet — call knowcode_lifecycle "
                "action='build' to create it",
                err=True,
            )

        # Run the server (blocking)
        run_server(store_path, config_path=config, include_legacy_tools=legacy_tools)

    except ImportError as e:
        click.echo(
            f"Error: MCP package not installed. Install with: pip install mcp\n{e}",
            err=True,
        )
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
