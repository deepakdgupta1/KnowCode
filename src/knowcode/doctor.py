"""Readiness checks for local KnowCode installations."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shlex
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from knowcode.config import AppConfig
from knowcode.data_models import EmbeddingConfig
from knowcode.indexing.indexer import Indexer
from knowcode.readiness import (
    IDEAL_SETUP_FEATURE_KEYS,
    IDEAL_SETUP_TARGET,
    install_hint,
    missing_features,
)
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.storage.sqlite_knowledge_store import SqliteKnowledgeStore
from knowcode.storage.vector_backends import inspect_vector_index


CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class DoctorSuggestion:
    """Actionable remediation suggestion for a readiness check."""

    label: str
    command: str
    detail: str = ""


@dataclass(frozen=True)
class DoctorCheck:
    """One readiness check result."""

    name: str
    status: CheckStatus
    message: str
    hint: Optional[str] = None
    suggestions: tuple[DoctorSuggestion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReadinessContext:
    """Resolved paths and config shared across doctor checks."""

    cwd: Path
    store_path: Path
    store_file: Path
    store_root: Path
    index_path: Path
    config_path: Optional[Path]
    config: AppConfig
    vector_backend: str


@dataclass(frozen=True)
class DoctorReport:
    """Complete doctor result."""

    checks: list[DoctorCheck]

    @property
    def ok(self) -> bool:
        """Return True when no check failed."""
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report for JSON output."""
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


def run_doctor(
    *,
    store_path: str | Path = ".",
    index_path: str | Path | None = None,
    config_path: str | Path | None = None,
    max_disk_mb: float = 500.0,
    include_mcp: bool = False,
    mcp_timeout_seconds: float = 5.0,
) -> DoctorReport:
    """Run local readiness checks for KnowCode.

    Args:
        store_path: Knowledge store file or directory.
        index_path: Semantic index directory. Defaults to ``knowcode_index``
            beside the knowledge store.
        config_path: Optional explicit ``aimodels.yaml`` path.
        max_disk_mb: Disk footprint warning threshold.
        include_mcp: Whether to spawn the MCP stdio server and validate a
            list-tools/tool-call roundtrip.
        mcp_timeout_seconds: Timeout for MCP handshake operations.
    """
    checks: list[DoctorCheck] = []

    config, resolved_config = _check_config(config_path, checks)
    store_file = _resolve_store_file(store_path)
    store_root = store_file.parent
    resolved_index = Path(index_path) if index_path is not None else store_root / "knowcode_index"
    context = ReadinessContext(
        cwd=Path.cwd(),
        store_path=Path(store_path),
        store_file=store_file,
        store_root=store_root,
        index_path=resolved_index,
        config_path=resolved_config,
        config=config,
        vector_backend=config.vector_backend,
    )

    _check_api_keys(context.config, checks)
    _check_knowledge_store(context, checks)

    _check_dependencies(checks)
    _check_python_runtime(checks)

    _check_semantic_index(context, checks)

    _check_disk_footprint(
        store_file=context.store_file,
        index_path=context.index_path,
        max_disk_mb=max_disk_mb,
        checks=checks,
    )

    _check_agent_rules(context.store_root, checks)
    _check_unsupported_languages(context.store_root, checks)
    _check_freshness(context, checks)

    if include_mcp:
        checks.append(
            _run_mcp_check(
                store_path=context.store_file,
                config_path=context.config_path,
                timeout_seconds=mcp_timeout_seconds,
            )
        )

    return DoctorReport(checks=checks)


def _shell_command(parts: list[str]) -> str:
    """Return a shell-safe command string for human-facing suggestions."""
    return shlex.join(parts)


def _check_config(
    config_path: str | Path | None,
    checks: list[DoctorCheck],
) -> tuple[AppConfig, Optional[Path]]:
    """Validate configuration under strict mode and return the loaded config."""
    resolved = _resolve_config_path(config_path)
    if config_path is not None and resolved is None:
        requested = Path(config_path)
        checks.append(
            DoctorCheck(
                name="Config",
                status="fail",
                message=f"Config file not found: {requested}",
                hint="Pass a valid --config path or create aimodels.yaml.",
                suggestions=(
                    DoctorSuggestion(
                        label="Create config file",
                        command="touch aimodels.yaml",
                        detail="Add explicit model and API key settings before model-backed commands.",
                    ),
                ),
            )
        )
        return AppConfig.default(), None

    try:
        config = AppConfig.load(str(resolved) if resolved else None, strict=True)
    except Exception as exc:
        checks.append(
            DoctorCheck(
                name="Config",
                status="fail",
                message=str(exc),
                hint="Fix the config file or run without --config to use defaults.",
            )
        )
        return AppConfig.default(), resolved

    if resolved is None:
        checks.append(
            DoctorCheck(
                name="Config",
                status="warn",
                message="No config file found; using built-in defaults.",
                hint="Create aimodels.yaml for explicit model and API key settings.",
                suggestions=(
                    DoctorSuggestion(
                        label="Create config file",
                        command="touch aimodels.yaml",
                    ),
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Config",
                status="pass",
                message=(
                    f"Loaded {resolved} "
                    f"({len(config.models)} LLM, "
                    f"{len(config.embedding_models)} embedding, "
                    f"{len(config.reranking_models)} reranking models)."
                ),
            )
        )
    return config, resolved


def _check_api_keys(config: AppConfig, checks: list[DoctorCheck]) -> None:
    """Check environment variables required by configured models."""
    env_names = sorted(
        {
            model.api_key_env
            for model in (
                list(config.models)
                + list(config.embedding_models)
                + list(config.reranking_models)
            )
            if model.api_key_env
        }
    )
    if not env_names:
        checks.append(
            DoctorCheck(
                name="API keys",
                status="warn",
                message="No model API key environment variables are configured.",
                hint="Add model entries to aimodels.yaml if you need LLM or semantic search.",
            )
        )
        return

    missing = [name for name in env_names if not os.environ.get(name)]
    if missing:
        checks.append(
            DoctorCheck(
                name="API keys",
                status="fail",
                message=f"Missing environment variables: {', '.join(missing)}.",
                hint="Export the missing keys before using indexing, reranking, or LLM Q&A.",
                suggestions=tuple(
                    DoctorSuggestion(
                        label=f"Export {name}",
                        command=f'export {name}="..."',
                        detail="Set this in your shell before running KnowCode model-backed commands.",
                    )
                    for name in missing
                ),
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="API keys",
            status="pass",
            message=f"Found {len(env_names)} configured API key environment variables.",
        )
    )


def _check_dependencies(checks: list[DoctorCheck]) -> None:
    """Check for optional and native dependencies."""
    # FAISS is special - it's a native binary
    faiss_installed = True
    try:
        importlib.import_module("faiss")
    except (ImportError, RuntimeError):
        faiss_installed = False

    if not faiss_installed:
        checks.append(
            DoctorCheck(
                name="Native dependencies",
                status="warn",
                message="FAISS native binaries not found. Using MockVectorStore fallback.",
                hint='Install with `pip install "faiss-cpu>=1.7.0"` or `knowcode install`.',
                suggestions=(
                    DoctorSuggestion(
                        label="Install ideal setup",
                        command="knowcode install",
                        detail="Includes FAISS plus the other optional KnowCode integrations.",
                    ),
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Native dependencies",
                status="pass",
                message="FAISS native binaries are installed and functional.",
            )
        )

    missing = missing_features(IDEAL_SETUP_FEATURE_KEYS)
    if missing:
        missing_labels = ", ".join(
            f"{item.feature.key} ({', '.join(item.modules)})" for item in missing
        )
        msg = f"Missing {len(missing)} optional features: {missing_labels}."
        checks.append(
            DoctorCheck(
                name="Optional dependencies",
                status="warn",
                message=msg,
                hint=install_hint(),
                suggestions=(
                    DoctorSuggestion(
                        label="Install ideal setup",
                        command="knowcode install",
                        detail=f'Equivalent package target: pip install "{IDEAL_SETUP_TARGET}"',
                    ),
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Optional dependencies",
                status="pass",
                message="All optional dependencies are installed.",
            )
        )


def _check_python_runtime(checks: list[DoctorCheck]) -> None:
    """Warn when running on a Python version known to break uvx resolution."""
    if sys.version_info < (3, 13):
        return

    checks.append(
        DoctorCheck(
            name="Python runtime",
            status="warn",
            message=(
                "Python 3.13+ may not resolve tree-sitter-languages wheels "
                "when KnowCode is launched through uvx."
            ),
            hint="Use `uvx --python 3.12 ...` for ephemeral KnowCode tool runs.",
            suggestions=(
                DoctorSuggestion(
                    label="Run with Python 3.12",
                    command=f'uvx --python 3.12 --from "{IDEAL_SETUP_TARGET}" knowcode doctor',
                    detail="tree-sitter-languages publishes wheels through CPython 3.12.",
                ),
            ),
        )
    )


def _check_knowledge_store(context: ReadinessContext, checks: list[DoctorCheck]) -> None:
    """Validate knowledge store presence and schema."""
    store_file = context.store_file
    if not store_file.exists():
        checks.append(
            DoctorCheck(
                name="Knowledge store",
                status="fail",
                message=f"Knowledge store not found: {store_file}",
                hint="Run `knowcode build <dir>` first.",
                suggestions=(
                    DoctorSuggestion(
                        label="Build KnowCode artifacts",
                        command=f"knowcode build {context.store_root}",
                        detail="Creates the knowledge store and semantic index for this project.",
                    ),
                ),
            )
        )
        return

    if store_file.suffix == ".db":
        try:
            connection = sqlite3.connect(
                f"file:{store_file.resolve()}?mode=ro",
                uri=True,
            )
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                required_tables = {"entities", "relationships"}
                if not required_tables.issubset(tables):
                    raise ValueError("missing entities or relationships table")
                entity_count = int(
                    connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                )
                relationship_count = int(
                    connection.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
                )
            finally:
                connection.close()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            checks.append(
                DoctorCheck(
                    name="Knowledge store",
                    status="fail",
                    message=f"Could not load {store_file}: {exc}",
                    hint="Re-run `knowcode build <dir>` to rebuild the store.",
                )
            )
            return
        checks.append(
            DoctorCheck(
                name="Knowledge store",
                status="pass",
                message=(
                    f"Loaded {store_file} "
                    f"(SQLite schema v{SqliteKnowledgeStore.SCHEMA_VERSION}, "
                    f"{entity_count} entities, "
                    f"{relationship_count} relationships)."
                ),
            )
        )
        return

    try:
        raw = _read_json_object(store_file)
        store = KnowledgeStore.load(store_file)
    except Exception as exc:
        checks.append(
            DoctorCheck(
                name="Knowledge store",
                status="fail",
                message=f"Could not load {store_file}: {exc}",
                hint="Re-run `knowcode build <dir>` to rebuild the store.",
                suggestions=(
                    DoctorSuggestion(
                        label="Rebuild knowledge store",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
        return

    schema_version = raw.get("schema_version")
    if schema_version is None or int(schema_version) != KnowledgeStore.SCHEMA_VERSION:
        checks.append(
            DoctorCheck(
                name="Knowledge store",
                status="warn",
                message=(
                    f"Loaded {store_file} via schema migration "
                    f"({len(store.entities)} entities)."
                ),
                hint="Re-run `knowcode build <dir>` to persist the current schema.",
                suggestions=(
                    DoctorSuggestion(
                        label="Persist current schema",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="Knowledge store",
            status="pass",
            message=(
                f"Loaded {store_file} "
                f"(schema v{schema_version}, {len(store.entities)} entities, "
                f"{len(store.relationships)} relationships)."
            ),
        )
    )


def _check_semantic_index(context: ReadinessContext, checks: list[DoctorCheck]) -> None:
    """Validate semantic index files, schemas, and embedding dimensions."""
    index_path = context.index_path
    if not index_path.exists():
        checks.append(
            DoctorCheck(
                name="Semantic index",
                status="fail",
                message=f"Semantic index not found: {index_path}",
                hint="Run `knowcode build <dir>` first.",
                suggestions=(
                    DoctorSuggestion(
                        label="Build semantic index",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
        return
    if not index_path.is_dir():
        checks.append(
            DoctorCheck(
                name="Semantic index",
                status="fail",
                message=f"Semantic index path is not a directory: {index_path}",
                hint="Pass --index pointing at a KnowCode index directory.",
                suggestions=(
                    DoctorSuggestion(
                        label="Run doctor with explicit index",
                        command=f"knowcode doctor --store {context.store_root} --index {context.index_path}",
                    ),
                ),
            )
        )
        return

    failures: list[str] = []
    warnings: list[str] = []

    manifest_file = index_path / "index_manifest.json"

    manifest: dict[str, Any] = {}
    raw_manifest: dict[str, Any] = {}
    if manifest_file.exists():
        try:
            raw_manifest = _read_json_object(manifest_file)
            manifest = Indexer._validate_and_migrate_manifest(raw_manifest)
        except Exception as exc:
            failures.append(f"invalid manifest: {exc}")
    else:
        failures.append("missing index_manifest.json")

    vector_inspection = inspect_vector_index(
        index_path,
        configured_backend=context.vector_backend,
    )
    failures.extend(vector_inspection.failures)
    warnings.extend(vector_inspection.warnings)
    vector_metadata = vector_inspection.metadata

    expected_embedding = _expected_embedding_config(context.config)
    recorded_embedding = manifest.get("embedding", {})
    if isinstance(recorded_embedding, dict):
        for key in ("provider", "model_name", "dimension", "normalize"):
            if key not in recorded_embedding:
                continue
            recorded = recorded_embedding.get(key)
            expected = getattr(expected_embedding, key, None)
            if recorded != expected:
                failures.append(f"{key} mismatch: index={recorded!r} current={expected!r}")

    recorded_dimension = vector_metadata.get("dimension")
    if recorded_dimension is not None:
        try:
            actual_dimension = int(recorded_dimension)
        except (TypeError, ValueError):
            failures.append(f"invalid vector dimension: {recorded_dimension!r}")
        else:
            if actual_dimension != expected_embedding.dimension:
                failures.append(
                    "vector dimension mismatch: "
                    f"index={recorded_dimension!r} current={expected_embedding.dimension!r}"
                )

    if raw_manifest and not _schema_is_current(raw_manifest, Indexer.SCHEMA_VERSION):
        warnings.append("manifest was migrated in memory")

    if failures:
        checks.append(
            DoctorCheck(
                name="Semantic index",
                status="fail",
                message=f"{index_path}: {'; '.join(failures)}.",
                hint="Rebuild the semantic index with `knowcode build <dir>`.",
                suggestions=(
                    DoctorSuggestion(
                        label="Rebuild semantic index",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
        return

    if warnings:
        checks.append(
            DoctorCheck(
                name="Semantic index",
                status="warn",
                message=f"{index_path}: {'; '.join(warnings)}.",
                hint="Rebuild the semantic index to persist the current schema.",
                suggestions=(
                    DoctorSuggestion(
                        label="Persist current index schema",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="Semantic index",
            status="pass",
            message=(
                f"Loaded {index_path} "
                f"(schema v{manifest.get('schema_version')}, "
                f"{expected_embedding.provider}/{expected_embedding.model_name}, "
                f"dimension {expected_embedding.dimension})."
            ),
        )
    )


def _check_disk_footprint(
    *,
    store_file: Path,
    index_path: Path,
    max_disk_mb: float,
    checks: list[DoctorCheck],
) -> None:
    """Warn when persisted KnowCode artifacts exceed the configured cap."""
    total_bytes = 0
    if store_file.exists():
        total_bytes += store_file.stat().st_size
    if index_path.exists():
        total_bytes += _path_size(index_path)

    total_mb = total_bytes / (1024 * 1024)
    if total_mb > max_disk_mb:
        checks.append(
            DoctorCheck(
                name="Disk footprint",
                status="warn",
                message=f"Artifacts use {total_mb:.2f} MB, above the {max_disk_mb:.2f} MB cap.",
                hint="Raise --max-disk-mb or rebuild with a narrower source tree.",
            )
        )
        return

    checks.append(
        DoctorCheck(
            name="Disk footprint",
            status="pass",
            message=f"Artifacts use {total_mb:.2f} MB, under the {max_disk_mb:.2f} MB cap.",
        )
    )


def _run_mcp_check(
    *,
    store_path: str | Path,
    config_path: Optional[Path],
    timeout_seconds: float,
) -> DoctorCheck:
    """Run the MCP stdio handshake in a subprocess."""
    try:
        return asyncio.run(
            _check_mcp_handshake(
                store_path=store_path,
                config_path=config_path,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:
        return DoctorCheck(
            name="MCP handshake",
            status="fail",
            message=f"MCP handshake failed: {exc}",
            hint="Install knowcode[mcp] and run `knowcode mcp-server --store <path>` to debug.",
        )


async def _check_mcp_handshake(
    *,
    store_path: str | Path,
    config_path: Optional[Path],
    timeout_seconds: float,
) -> DoctorCheck:
    """Spawn the MCP server, list tools, call one tool, and parse the response."""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        return DoctorCheck(
            name="MCP handshake",
            status="fail",
            message=f"MCP client package is not installed: {exc}",
            hint='Install the optional extra with `pip install "knowcode[mcp]"`.',
        )

    args = ["-m", "knowcode.cli.cli", "mcp-server", "--store", str(store_path)]
    if config_path is not None:
        args.extend(["--config", str(config_path)])

    params = StdioServerParameters(
        command=sys.executable,
        args=args,
        cwd=Path.cwd(),
    )
    import tempfile

    with tempfile.TemporaryFile(mode="w+t") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
                tools_result = await asyncio.wait_for(
                    session.list_tools(),
                    timeout=timeout_seconds,
                )
                tool_names = {tool.name for tool in tools_result.tools}
                if "search_codebase" not in tool_names:
                    return DoctorCheck(
                        name="MCP handshake",
                        status="fail",
                        message="MCP server did not expose search_codebase.",
                        hint="Check src/knowcode/mcp/server.py tool registration.",
                    )

                call_result = await asyncio.wait_for(
                    session.call_tool("search_codebase", {"query": "", "limit": 1}),
                    timeout=timeout_seconds,
                )
                if not call_result.content:
                    return DoctorCheck(
                        name="MCP handshake",
                        status="fail",
                        message="MCP tool call returned no content.",
                        hint="Run `knowcode mcp-server --store <path>` manually to inspect stderr.",
                    )

                text = getattr(call_result.content[0], "text", "")
                parsed = json.loads(text)
                if not isinstance(parsed, list):
                    return DoctorCheck(
                        name="MCP handshake",
                        status="fail",
                        message="MCP tool response was not a JSON list.",
                        hint="Check search_codebase response formatting.",
                    )

        errlog.seek(0)
        stderr = errlog.read().strip()
    message = f"Listed {len(tool_names)} tools and called search_codebase successfully."
    if stderr:
        message = f"{message} Server stderr: {stderr}"
    return DoctorCheck(name="MCP handshake", status="pass", message=message)


def _resolve_store_file(store_path: str | Path) -> Path:
    """Resolve a store argument to the current SQLite or legacy JSON path."""
    path = Path(store_path)
    if path.is_dir():
        sqlite_store = path / "knowledge.db"
        if sqlite_store.exists():
            return sqlite_store
        return path / KnowledgeStore.DEFAULT_FILENAME
    if path.suffix in {".json", ".db"}:
        return path
    return path / KnowledgeStore.DEFAULT_FILENAME


def _resolve_config_path(config_path: str | Path | None) -> Optional[Path]:
    """Resolve explicit, local, or home config path."""
    if config_path is not None:
        path = Path(config_path)
        return path if path.exists() else None

    local = Path("aimodels.yaml")
    if local.exists():
        return local

    home = Path.home() / ".aimodels.yaml"
    if home.exists():
        return home

    return None


def _expected_embedding_config(config: AppConfig) -> EmbeddingConfig:
    """Return the embedding config implied by AppConfig without reading env vars."""
    if not config.embedding_models:
        return EmbeddingConfig()

    model = config.embedding_models[0]
    provider = model.provider.lower()
    if provider in {"voyageai", "voyage"}:
        return EmbeddingConfig(
            provider="voyageai",
            model_name=model.name,
            dimension=_voyage_dimension(model.name),
        )

    return EmbeddingConfig(
        provider="openai",
        model_name=model.name,
        dimension=_openai_dimension(model.name),
    )


def _voyage_dimension(model_name: str) -> int:
    """Return known VoyageAI embedding dimension."""
    try:
        from knowcode.llm.embedding import _VOYAGE_EMBED_DIMENSIONS
    except ImportError:
        return 1024
    return int(_VOYAGE_EMBED_DIMENSIONS.get(model_name, 1024))


def _openai_dimension(model_name: str) -> int:
    """Return known OpenAI-compatible embedding dimension."""
    try:
        from knowcode.llm.embedding import _OPENAI_EMBED_DIMENSIONS
    except ImportError:
        return 1536
    return int(_OPENAI_EMBED_DIMENSIONS.get(model_name, 1536))


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}.")
    return data


def _schema_is_current(payload: dict[str, Any], current: int) -> bool:
    """Return True when a JSON payload declares the current schema version."""
    value = payload.get("schema_version")
    return value == current or value == str(current)


def _path_size(path: Path) -> int:
    """Return total bytes under a file or directory."""
    if path.is_file():
        return path.stat().st_size

    total = 0
    import logging
    logger = logging.getLogger(__name__)
    
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError as e:
            logger.warning("Skipped file size calculation for %s: %s", child, e)
            
    return total


def _check_agent_rules(store_path: str | Path, checks: list[DoctorCheck]) -> None:
    """Validate presence of the agent rule file."""
    store_root = Path(store_path).resolve()
    if not store_root.is_dir():
        store_root = store_root.parent

    rules_file = store_root / ".agent/rules/context.md"
    if rules_file.exists():
        checks.append(
            DoctorCheck(
                name="Agent rules",
                status="pass",
                message=f"Found active agent rule file: {rules_file.name}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Agent rules",
                status="warn",
                message="No active agent rule file found (e.g. .agent/rules/context.md).",
                hint="Create .agent/rules/context.md containing the canonical MCP contract reference to guide agent actions.",
            )
        )


def _check_unsupported_languages(store_path: str | Path, checks: list[DoctorCheck]) -> None:
    """Scan directory for common unsupported source code extensions."""
    store_root = Path(store_path).resolve()
    if not store_root.is_dir():
        store_root = store_root.parent

    unsupported_exts = set()
    unsupported_map = {
        ".go": "Go",
        ".cpp": "C++",
        ".c": "C",
        ".h": "C/C++ Header",
        ".swift": "Swift",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
    }
    ignored_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}

    try:
        for root, dirs, files in os.walk(store_root):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext in unsupported_map:
                    unsupported_exts.add(unsupported_map[ext])
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to scan for unsupported languages: %s", e)

    if unsupported_exts:
        checks.append(
            DoctorCheck(
                name="Supported languages",
                status="warn",
                message=f"Detected unsupported codebase languages: {', '.join(sorted(unsupported_exts))}.",
                hint="KnowCode only indexes supported languages (Python, JS/TS, Java, Rust, Vue, Markdown, YAML).",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Supported languages",
                status="pass",
                message="No unsupported source code extensions detected.",
            )
        )


def _check_freshness(context: ReadinessContext, checks: list[DoctorCheck]) -> None:
    """Validate freshness of the knowledge store and index."""
    try:
        from knowcode.service import KnowCodeService
        service = KnowCodeService(
            store_path=context.store_root,
            config_path=str(context.config_path) if context.config_path else None,
        )
        freshness = service.get_freshness_metadata()
        if freshness["is_stale"]:
            checks.append(
                DoctorCheck(
                    name="Freshness",
                    status="warn",
                    message=f"Store/index may be stale. Reasons: {', '.join(freshness['stale_reasons'])}.",
                    hint="Re-run `knowcode build` to rebuild artifacts.",
                    suggestions=(
                        DoctorSuggestion(
                            label="Rebuild stale artifacts",
                            command=f"knowcode build {context.store_root}",
                        ),
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Freshness",
                    status="pass",
                    message="Knowledge store and semantic index are fresh and match the source tree.",
                )
            )
    except Exception as exc:
        checks.append(
            DoctorCheck(
                name="Freshness",
                status="fail",
                message=f"Could not determine freshness: {exc}",
                hint="Ensure the knowledge store and semantic index are built.",
                suggestions=(
                    DoctorSuggestion(
                        label="Build KnowCode artifacts",
                        command=f"knowcode build {context.store_root}",
                    ),
                ),
            )
        )
