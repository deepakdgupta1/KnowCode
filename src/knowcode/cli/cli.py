"""CLI interface for KnowCode."""

import json
import sys
from pathlib import Path
from typing import Any, Optional

import click

from knowcode import __version__
from knowcode.analysis.documentation_synthesizer import DocumentationSynthesizer
from knowcode.data_models import RelationshipKind
from knowcode.errors import KnowCodePrerequisiteError
from knowcode.service import KnowCodeService
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.utils.dependency_guard import require_extra


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
    if stats.get("indexed_chunks") is not None:
        click.echo(f"  Indexed chunks: {stats['indexed_chunks']}")
    if stats.get("index_error"):
        click.echo("  Indexing warning: semantic index build skipped.", err=True)
        click.echo(f"    {stats['index_error']}", err=True)

    output_path = Path(output)
    save_path = output_path / KnowledgeStore.DEFAULT_FILENAME if output_path.is_dir() else output_path
    click.echo(f"\n  Saved to: {save_path}")
    if stats.get("index_path"):
        click.echo(f"  Index saved to: {stats['index_path']}")


@cli.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    required=False,
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
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for embedding models.",
)
def build(
    directory: str,
    ignore: tuple[str, ...],
    temporal: bool,
    config: Optional[str],
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
    )

    click.echo("\n✓ Build complete!")
    click.echo(f"  Entities: {stats['total_entities']}")
    click.echo(f"  Relationships: {stats['total_relationships']}")
    if stats.get("total_errors", 0) > 0:
        click.echo(f"  Errors: {stats['total_errors']}")
    if stats.get("indexed_chunks") is not None:
        click.echo(f"  Indexed chunks: {stats['indexed_chunks']}")

    save_path = target / KnowledgeStore.DEFAULT_FILENAME
    click.echo(f"\n  Knowledge store: {save_path}")
    if stats.get("index_path"):
        click.echo(f"  Semantic index: {stats['index_path']}")
    if stats.get("index_error"):
        click.echo(
            "\n  Note: semantic index was skipped (knowledge store still built).",
            err=True,
        )
        click.echo(f"    {stats['index_error']}", err=True)
        click.echo(
            "    Build it later with `knowcode index` once embeddings are configured.",
            err=True,
        )


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
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (aimodels.yaml) for embedding models.",
)
def index(directory: str, output: str, config: Optional[str]) -> None:
    """Build semantic search index for a codebase.

    DIRECTORY: Path to the codebase to index.
    """
    click.echo(f"Indexing: {directory}")

    try:
        require_extra("search", "knowcode index", ("faiss", "numpy"))
        from knowcode.config import AppConfig
        from knowcode.llm.embedding import create_embedding_provider
        from knowcode.indexing.indexer import Indexer

        app_config = AppConfig.load(config)
        provider = create_embedding_provider(app_config=app_config)
        indexer = Indexer(provider)

        count = indexer.index_directory(directory)
        indexer.save(output)

        click.echo(f"✓ Indexing complete! Created {count} chunks.")
        click.echo(f"  Saved to: {output}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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
        click.echo("Error: Knowledge store not found. Run 'knowcode build' first.", err=True)
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
    "--config", "-c",
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
        require_extra("search", "knowcode semantic-search", ("faiss", "numpy"))
        from knowcode.config import AppConfig

        app_config = AppConfig.load(config)
        service = KnowCodeService(store_path=store, app_config=app_config)
        engine = service.get_search_engine(index_path=index)

        results = engine.search(question, limit=limit)

        if not results:
            click.echo("No relevant code found.")
        else:
            for i, chunk in enumerate(results):
                click.echo(f"\n[{i+1}] {chunk.entity_id}")
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
        click.echo("Error: Knowledge store not found. Run 'knowcode build' first.", err=True)
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
    """Export knowledge store as multi-level Markdown documentation."""
    try:
        service = KnowCodeService(store_path=store)
    except FileNotFoundError:
        click.echo("Error: Knowledge store not found. Run 'knowcode build' first.", err=True)
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
        click.echo("Error: Knowledge store not found. Run 'knowcode build' first.", err=True)
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
    type=click.Path(),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--index", "-i",
    "index_path",
    type=click.Path(),
    help="Path to semantic index directory (default: beside the store)",
)
@click.option(
    "--config", "-c",
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
    "--json", "as_json",
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
        click.echo("Error: Knowledge store not found. Run 'knowcode build' first.", err=True)
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
                if rel.kind == RelationshipKind.AUTHORED:
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
            if rel.kind == RelationshipKind.CHANGED_BY:
                commit = knowledge.get_entity(rel.target_id)  # type: ignore
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
            click.echo(f"  {date} {commit.name} {stats}: {commit.docstring.splitlines()[0]}")  # type: ignore


@cli.command()
@click.argument("query_text", nargs=-1, required=True)
@click.option(
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file for model priorities",
)
def ask(query_text: tuple[str], store: str, config: Optional[str]) -> None:
    """Ask a question about the codebase using AI.
    
    QUERY_TEXT: The question to ask.
    """
    question = " ".join(query_text)
    
    try:
        require_extra("llm", "knowcode ask", ("openai", "google.genai", "google.api_core"))
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
    "--store", "-s",
    type=click.Path(exists=True),
    default=".",
    help="Path to knowledge store file or directory",
)
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file for model priorities",
)
def mcp_server(store: str, config: Optional[str]) -> None:
    """Start MCP server for IDE integration.
    
    Exposes KnowCode tools via the Model Context Protocol (MCP) using
    STDIO transport. Four tools are available:
    
    \b
    - search_codebase: Search for code entities by name
    - get_entity_context: Get detailed context for an entity  
    - trace_calls: Trace call graph (callers/callees) with depth
    - retrieve_context_for_query: Unified query-to-context retrieval bundle
    
    Example usage with Claude Desktop or other MCP clients:
    
    \b
        # In your MCP client config, add:
        {
            "knowcode": {
                "command": "knowcode",
                "args": ["mcp-server", "--store", "/path/to/project"]
            }
        }
    """
    store_path = Path(store)
    store_file = store_path / KnowledgeStore.DEFAULT_FILENAME if store_path.is_dir() else store_path
    if not store_file.exists():
        click.echo(
            "Error: Knowledge store not found. Run `knowcode build <dir>` first.",
            err=True,
        )
        sys.exit(1)
    
    try:
        from knowcode.mcp.server import run_server
        
        click.echo("🔌 Starting MCP server...", err=True)
        click.echo(f"   Store: {store_path}", err=True)
        click.echo("   Transport: STDIO", err=True)
        click.echo(
            "   Tools: search_codebase, get_entity_context, trace_calls, retrieve_context_for_query",
            err=True,
        )
        
        # Run the server (blocking)
        run_server(store_path, config_path=config)
        
    except ImportError as e:
        click.echo(
            f"Error: MCP package not installed. Install with: pip install mcp\n{e}",
            err=True
        )
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
