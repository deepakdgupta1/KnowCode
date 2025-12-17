"""CLI interface for KnowCode."""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from knowcode import __version__
from knowcode.context_synthesizer import ContextSynthesizer
from knowcode.graph_builder import GraphBuilder
from knowcode.knowledge_store import KnowledgeStore
from knowcode.models import EntityKind


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

    # Build graph
    builder = GraphBuilder()
    builder.build_from_directory(
        root_dir=directory,
        additional_ignores=list(ignore),
        analyze_temporal=temporal,
        coverage_path=Path(coverage) if coverage else None,
    )

    # Create store and save
    store = KnowledgeStore.from_graph_builder(builder)
    output_path = Path(output)
    store.save(output_path)

    # Print summary
    stats = builder.stats()
    click.echo("\n✓ Analysis complete!")
    click.echo(f"  Entities: {stats['total_entities']}")
    click.echo(f"  Relationships: {stats['total_relationships']}")
    if stats['total_errors'] > 0:
        click.echo(f"  Errors: {stats['total_errors']}")

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
        knowledge = KnowledgeStore.load(store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    results: list[dict[str, str]] = []

    if query_type == "search":
        entities = knowledge.search(target)
        for e in entities:
            results.append({
                "id": e.id,
                "kind": e.kind.value,
                "name": e.qualified_name,
                "file": e.location.file_path,
                "line": str(e.location.line_start),
            })

    elif query_type == "callers":
        entity = knowledge.get_entity(target)
        if not entity:
            # Try searching
            matches = knowledge.search(target)
            if matches:
                entity = matches[0]
                click.echo(f"Using: {entity.id}")

        if entity:
            callers = knowledge.get_callers(entity.id)
            for c in callers:
                results.append({
                    "id": c.id,
                    "name": c.qualified_name,
                    "file": c.location.file_path,
                })

    elif query_type == "callees":
        entity = knowledge.get_entity(target)
        if not entity:
            matches = knowledge.search(target)
            if matches:
                entity = matches[0]
                click.echo(f"Using: {entity.id}")

        if entity:
            callees = knowledge.get_callees(entity.id)
            for c in callees:
                results.append({
                    "id": c.id,
                    "name": c.qualified_name,
                })

    elif query_type == "deps":
        entity = knowledge.get_entity(target)
        if not entity:
            matches = knowledge.search(target)
            if matches:
                entity = matches[0]
                click.echo(f"Using: {entity.id}")

        if entity:
            deps = knowledge.get_dependencies(entity.id)
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
        knowledge = KnowledgeStore.load(store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    synthesizer = ContextSynthesizer(knowledge, max_tokens=max_tokens)

    # Try exact match first
    entity = knowledge.get_entity(target)
    if not entity:
        # Try search
        matches = knowledge.search(target)
        if matches:
            entity = matches[0]
            click.echo(f"Using: {entity.id}\n", err=True)

    if not entity:
        click.echo(f"Entity not found: {target}", err=True)
        sys.exit(1)

    bundle = synthesizer.synthesize(entity.id)
    if bundle:
        click.echo(bundle.context_text)
        click.echo(f"\n--- {bundle.total_chars} chars, {bundle.total_tokens} tokens, {len(bundle.included_entities)} entities ---", err=True)
        if bundle.truncated:
            click.echo("(truncated)", err=True)


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
        knowledge = KnowledgeStore.load(store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

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
        knowledge = KnowledgeStore.load(store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)

    click.echo("Knowledge Store Statistics")
    click.echo("-" * 30)

    # Count by kind
    by_kind: dict[str, int] = {}
    for entity in knowledge.entities.values():
        kind = entity.kind.value
        by_kind[kind] = by_kind.get(kind, 0) + 1

    click.echo(f"\nTotal Entities: {len(knowledge.entities)}")
    for kind, count in sorted(by_kind.items()):
        click.echo(f"  {kind}: {count}")

    click.echo(f"\nTotal Relationships: {len(knowledge.relationships)}")

    # Relationship types
    rel_types: dict[str, int] = {}
    for rel in knowledge.relationships:
        kind = rel.kind.value
        rel_types[kind] = rel_types.get(kind, 0) + 1

    for kind, count in sorted(rel_types.items()):
        click.echo(f"  {kind}: {count}")



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
        knowledge = KnowledgeStore.load(store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode analyze' first.", err=True)
        sys.exit(1)
        
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
