"""CLI interface for KnowCode."""

import json
import sys
from pathlib import Path
from typing import Any, Optional

import click

from knowcode import __version__
from knowcode.models import EntityKind
from knowcode.service import KnowCodeService
from knowcode.knowledge_store import KnowledgeStore


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """KnowCode - Transform your codebase into an effective knowledge base."""
    pass


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output", "-o",
    type=click.Path(),
    default=".",
    help="Output directory for knowledge store (default: current directory)",
)
@click.option(
    "--ignore", "-i",
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
def analyze(directory: str, output: str, ignore: tuple[str, ...], temporal: bool, coverage: Optional[str]) -> None:
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
    if stats.get('total_errors', 0) > 0:
        click.echo(f"  Errors: {stats['total_errors']}")

    output_path = Path(output)
    save_path = output_path / KnowledgeStore.DEFAULT_FILENAME if output_path.is_dir() else output_path
    click.echo(f"\n  Saved to: {save_path}")


@cli.command()
@click.argument("query_type", type=click.Choice(["callers", "callees", "deps", "search"]))
@click.argument("target")
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--json", "as_json",
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
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    results: list[dict[str, Any]] = []

    if query_type == "search":
        results = service.search(target)

    elif query_type == "callers":
        results = service.get_callers(target)

    elif query_type == "callees":
        results = service.get_callees(target)

    elif query_type == "deps":
        entity = service.store.get_entity(target) or next(iter(service.store.search(target)), None)
        if entity:
            deps = service.store.get_dependencies(entity.id)
            for d in deps:
                results.append({
                    "id": d.id,
                    "kind": d.kind.value,
                    "name": d.qualified_name,
                })

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


@cli.command()
@click.argument("target")
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--max-tokens", "-m",
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
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    try:
        bundle_dict = service.get_context(target, max_tokens=max_tokens)
        click.echo(bundle_dict["context_text"])
        click.echo(f"\n--- {len(bundle_dict['context_text'])} chars, {bundle_dict['total_tokens']} tokens, {len(bundle_dict['included_entities'])} entities ---", err=True)
        if bundle_dict["truncated"]:
            click.echo("(truncated)", err=True)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--output", "-o",
    type=click.Path(),
    default="docs",
    help="Output directory for documentation",
)
def export(store: str, output: str) -> None:
    """Export knowledge store as Markdown documentation."""
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    knowledge = service.store
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Export index
    index_lines = ["# Codebase Documentation", "", "Generated by KnowCode", "", "## Contents", ""]

    # Group entities by file
    by_file: dict[str, list] = {}
    for entity in knowledge.entities.values():
        file_path = entity.location.file_path
        if file_path not in by_file:
            by_file[file_path] = []
        by_file[file_path].append(entity)

    for file_path, entities in sorted(by_file.items()):
        index_lines.append(f"### `{Path(file_path).name}`")
        for e in sorted(entities, key=lambda x: x.location.line_start):
            if e.kind in {EntityKind.FUNCTION, EntityKind.CLASS, EntityKind.METHOD}:
                index_lines.append(f"- [{e.kind.value}] `{e.qualified_name}`")
                if e.docstring:
                    first_line = e.docstring.split("\n")[0][:80]
                    index_lines.append(f"  > {first_line}")
        index_lines.append("")

    # Write index
    index_path = output_dir / "index.md"
    index_path.write_text("\n".join(index_lines))

    click.echo(f"✓ Exported documentation to: {output_dir}")
    click.echo(f"  Index: {index_path}")


@cli.command()
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
def stats(store: str) -> None:
    """Show statistics about the knowledge store."""
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    s = service.get_stats()
    click.echo("Knowledge Store Statistics")
    click.echo("-" * 30)

    click.echo(f"\nTotal Entities: {s['total_entities']}")
    for kind, count in sorted(s['entities_by_kind'].items()):
        click.echo(f"  {kind}: {count}")

    click.echo(f"\nTotal Relationships: {s['total_relationships']}")
    for kind, count in sorted(s['relationships_by_type'].items()):
        click.echo(f"  {kind}: {count}")


@cli.command()
@click.option(
    "--store", "-s",
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
def server(store: str, host: str, port: int) -> None:
    """Start the KnowCode intelligence server."""
    from knowcode.server.main import start_server
    
    click.echo(f"Starting KnowCode server on {host}:{port}")
    click.echo(f"Using knowledge store: {store}")
    
    start_server(host=host, port=port, store_path=store)


@cli.command()
@click.argument("target", required=False)
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--limit", "-l",
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
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)
        
    knowledge = service.store
    
    if not target:
        # Show recent commits
        commits = knowledge.get_entities_by_kind("commit")
        # Sort by timestamp (metadata)
        commits.sort(key=lambda x: x.metadata.get("timestamp", "0"), reverse=True)
        
        click.echo(f"Recent History (showing {min(limit, len(commits))} of {len(commits)}):")
        for commit in commits[:limit]:
            date = commit.metadata.get("date", "Unknown date")
            author_rels = knowledge.get_incoming_relationships(commit.id)
            author = "Unknown"
            for rel in author_rels:
                if rel.kind == "authored":
                    # rel.source_id is author
                    a_ent = knowledge.get_entity(rel.source_id)
                    if a_ent:
                         author = a_ent.name
            
            click.echo(f"[{date}] {commit.name} - {author}")
            click.echo(f"  {commit.docstring.splitlines()[0] if commit.docstring else ''}")
            
    else:
        # Show history for specific entity
        entity = knowledge.get_entity(target)
        if not entity:
             matches = knowledge.search(target)
             if matches:
                 entity = matches[0]
                 click.echo(f"Using: {entity.id}\n")
        
        if not entity:
             click.echo(f"Entity not found: {target}")
             return

        click.echo(f"History for {entity.qualified_name} ({entity.kind.value}):")
        
        # Build history from relationships
        # Entity -> CHANGED_BY -> Commit
        rels = knowledge.get_outgoing_relationships(entity.id)
        changes = []
        for rel in rels:
            if rel.kind == "changed_by":
                commit = knowledge.get_entity(rel.target_id)
                if commit:
                    # Get modification stats from edge metadata
                    stats = f"(+{rel.metadata.get('insertions', 0)}/-{rel.metadata.get('deletions', 0)})"
                    timestamp = commit.metadata.get("timestamp", "0")
                    changes.append((timestamp, commit, stats))
        
        changes.sort(key=lambda x: x[0], reverse=True)
        
        if not changes:
            click.echo("  No recorded history (scan with --temporal).")
            return
            
        for _, commit, stats in changes[:limit]:
            date = commit.metadata.get("date", "")
            click.echo(f"  {date} {commit.name} {stats}: {commit.docstring.splitlines()[0]}")


if __name__ == "__main__":
    cli()
