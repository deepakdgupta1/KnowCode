"""MCP Server implementation for KnowCode.

Provides an MCP server that exposes KnowCode's codebase intelligence
to LLM applications via the Model Context Protocol.
"""

from __future__ import annotations

from typing import Any, Callable
import asyncio
import json
import threading
from pathlib import Path
from typing import Optional

# MCP imports - requires: pip install mcp
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        CallToolResult,
    )

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from knowcode.service import KnowCodeService
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.data_models import TaskType
from knowcode.errors import MissingKnowledgeStoreError, KnowCodePrerequisiteError
from knowcode.mcp import inspection, lifecycle
from knowcode.mcp.jobs import JobRegistry
from knowcode.mcp.legacy import UNHANDLED, dispatch_legacy
from knowcode.mcp.payloads import (
    chunk_to_dict,
    is_error_payload,
    logger,
    require_str,
    unknown_action,
)
from knowcode.mcp.roots import store_is_ready
from knowcode.mcp.tools import (
    CONSOLIDATED_TOOL_DEFINITIONS,
    INSPECT_ACTIONS,
    LIFECYCLE_ACTIONS,
    PRIMARY_TOOL_NAME,
    RETRIEVE_ACTIONS,
    tool_definitions,
)

#: Default surface. Re-exported from :mod:`knowcode.mcp.tools` so existing
#: importers of ``knowcode.mcp.server.TOOL_DEFINITIONS`` keep working.
TOOL_DEFINITIONS: list[dict[str, Any]] = CONSOLIDATED_TOOL_DEFINITIONS

__all__ = [
    "KnowCodeMCPServer",
    "TOOL_DEFINITIONS",
    "PRIMARY_TOOL_NAME",
    "create_server",
    "run_server",
    "run_server_async",
]


class KnowCodeMCPServer:
    """MCP Server wrapper for KnowCode.

    Owns three things: the repository root every action is scoped to, the
    lazily-loaded service, and the registry of background lifecycle jobs.

    The server starts whether or not a knowledge store exists. A missing
    store is reported per *action* rather than at startup, because refusing
    to boot without one makes the ``build`` action unreachable and leaves an
    agent in a fresh repository with no way forward but the terminal.
    """

    def __init__(
        self,
        store_path: str | Path,
        config_path: Optional[str] = None,
        jobs: Optional[JobRegistry] = None,
    ) -> None:
        """Initialize MCP server with knowledge store.

        Args:
            store_path: Repository root, or a path to a knowledge store file.
            config_path: Optional path to aimodels.yaml for model priorities.
            jobs: Job registry to use. Injected by tests; production uses a
                fresh thread-backed registry per server process.
        """
        self.store_path = Path(store_path)
        self.config_path = config_path
        self._service: Optional[KnowCodeService] = None
        # Guards ``_service`` against the lifecycle job thread. Without it, the
        # unlocked check-then-create let two callers build competing instances
        # and drop one without closing it.
        self._service_lock = threading.RLock()
        self._jobs = jobs if jobs is not None else JobRegistry()

    @property
    def root(self) -> Path:
        """Repository root that bounds every path an action may touch."""
        path = self.store_path
        return path.parent if path.is_file() else path

    def _legacy_store_file(self) -> Path:
        """Path the legacy flat-layout JSON store would occupy."""
        store_file = self.store_path
        if store_file.is_dir():
            store_file = store_file / KnowledgeStore.DEFAULT_FILENAME
        return store_file

    def _store_is_ready(self) -> bool:
        """Whether a readable knowledge store exists, in either layout."""
        return store_is_ready(self.store_path)

    def _new_service(self) -> KnowCodeService:
        """Construct a service rooted at this server's repository."""
        return KnowCodeService(
            store_path=self.store_path,
            config_path=self.config_path,
            strict_config=True,
        )

    def _ensure_service(self, allow_missing_store: bool = False) -> KnowCodeService:
        """Return the shared read service, creating it on first use."""
        with self._service_lock:
            if self._service is None:
                if not allow_missing_store and not self._store_is_ready():
                    raise MissingKnowledgeStoreError(self._legacy_store_file())
                self._service = self._new_service()
            return self._service

    def _dedicated_service(self) -> KnowCodeService:
        """Construct a service owned solely by one lifecycle job.

        A job must never share the cached read service. The publication step
        (``adopt_generation``) raises ``RepositoryClosedError`` if its service
        was closed mid-flight, so a foreground ``_invalidate_service()`` landing
        on a shared instance turned an already-published build into a reported
        failure — telling the agent to re-run a slow, quota-consuming operation
        that had in fact succeeded.

        The caller owns the returned service and must close it.
        """
        return self._new_service()

    def _service_factory(
        self, allow_missing_store: bool = False
    ) -> "Callable[[], KnowCodeService]":
        """Bind ``_ensure_service`` for read handlers that resolve it lazily."""
        return lambda: self._ensure_service(allow_missing_store=allow_missing_store)

    def _invalidate_service(self) -> None:
        """Drop and close the cached read service.

        Called before a lifecycle job starts and again when it finishes, so no
        read is answered from a generation the build has superseded. Only ever
        touches the shared instance: a job's dedicated service is invisible
        here, which is what makes closing safe while a job runs.
        """
        with self._service_lock:
            service = self._service
            self._service = None
        if service is not None:
            try:
                service.close()
            except Exception as exc:  # noqa: BLE001 - teardown must not mask a build
                logger().warning("Ignored error closing stale service: %s", exc)

    def search_codebase(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for entities by name pattern.

        Args:
            query: Search pattern.
            limit: Maximum results.

        Returns:
            List of matching entity summaries.
        """
        service = self._ensure_service()
        entities = service.store.search(query)[:limit]

        return [
            {
                "id": e.id,
                "name": e.name,
                "qualified_name": e.qualified_name,
                "kind": e.kind.value,
                "file": e.location.file_path,
                "line": e.location.line_start,
            }
            for e in entities
        ]

    def get_entity_context(
        self,
        entity_id: str,
        task_type: str = "general",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Get synthesized context for an entity.

        Args:
            entity_id: Entity ID or search pattern.
            task_type: Task type for prioritization.
            max_tokens: Token budget.

        Returns:
            Context bundle with sufficiency score.
        """
        service = self._ensure_service()
        try:
            task = TaskType(task_type)
        except ValueError:
            task = TaskType.GENERAL

        try:
            bundle = service.get_context(
                entity_id, max_tokens=max_tokens, task_type=task
            )
        except ValueError:
            return {
                "error": f"Entity not found: {entity_id}",
                "context_text": "",
                "sufficiency_score": 0.0,
                "task_type": task.value,
            }

        entity = service.store.get_entity(bundle.get("entity_id", entity_id))
        qualified_name = entity.qualified_name if entity else ""

        return {
            "entity_id": bundle.get("entity_id", entity_id),
            "qualified_name": qualified_name,
            "context_text": bundle.get("context_text", ""),
            "total_tokens": bundle.get("total_tokens", 0),
            "sufficiency_score": bundle.get("sufficiency_score", 0.0),
            "task_type": bundle.get("task_type", task.value),
        }

    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """Trace call graph from an entity.

        Args:
            entity_id: Starting entity.
            direction: "callers" or "callees".
            depth: Max hops.

        Returns:
            List of entities with call_depth.
        """
        service = self._ensure_service()
        from typing import cast

        return cast(
            list[dict[str, Any]],
            service.store.trace_calls(
                entity_id,
                direction=direction,
                depth=min(depth, 5),
                max_results=50,
            ),
        )

    def retrieve_context_for_query(
        self,
        query: str,
        task_type: str = "auto",
        max_tokens: int = 4000,
        limit_entities: int = 3,
        expand_deps: bool = True,
        verbosity: str = "minimal",
    ) -> dict[str, Any]:
        """Retrieve a task-aware context bundle for a query."""
        service = self._ensure_service()
        task_override: Optional[TaskType] = None
        if task_type != "auto":
            try:
                task_override = TaskType(task_type)
            except ValueError:
                task_override = TaskType.GENERAL

        return service.retrieve_context_for_query(
            query=query,
            max_tokens=max_tokens,
            task_type=task_override,
            limit_entities=limit_entities,
            expand_deps=expand_deps,
            verbosity=verbosity,
        )

    def assess_codebase_quality(self) -> dict[str, Any]:
        """Return the persisted pre-flight quality report for this codebase.

        Returns:
            The quality report dictionary, or an error dict if no report exists.
        """
        from knowcode.analysis.preflight_writer import load_preflight_report

        # Try loading from the current generation directory
        service = self._ensure_service()
        generation = service.current_generation()
        if generation is not None:
            report = load_preflight_report(generation.path)
            if report is not None:
                return report

        # Fall back to index root
        index_path = service.index_root
        report = load_preflight_report(index_path)
        if report is not None:
            return report

        return {
            "error": "No pre-flight report found. Run 'knowcode build' or "
            "'knowcode preflight' first.",
            "hint": "The pre-flight assessment runs automatically during "
            "'knowcode build'. You can also run 'knowcode preflight <dir>' "
            "for an ad-hoc assessment.",
        }

    # ------------------------------------------------------------------
    # Consolidated action surface
    # ------------------------------------------------------------------

    def semantic_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return raw ranked chunks for a query.

        Bypasses the sufficiency/freshness projection that ``query`` applies,
        so it is a follow-up tool rather than a first choice.
        """
        service = self._ensure_service()
        self._require_semantic_index(service)
        results = service.get_search_engine().search(query, limit=limit)
        return [
            result if isinstance(result, dict) else chunk_to_dict(result)
            for result in results
        ]

    @staticmethod
    def _require_semantic_index(service: KnowCodeService) -> None:
        """Fail before any call that would build an index as a side effect.

        Opening an indexer creates ``chunks.db`` and, on some paths,
        ``ensure_index`` runs a *full build*. A retrieval action must never
        silently spend minutes and embedding quota, so a missing index is a
        typed prerequisite error telling the agent to build explicitly.
        """
        from knowcode.errors import MissingSemanticIndexError

        generation = service.current_generation()
        if generation is not None and generation.has_semantic_index:
            return
        index_root = service.index_root
        if generation is None and (index_root / "index_manifest.json").exists():
            # Legacy flat layout with a real index beside the store.
            return
        raise MissingSemanticIndexError(index_root)

    def retrieve(self, action: str, arguments: dict[str, Any]) -> Any:
        """Route a ``knowcode_retrieve`` action. Read-only."""
        if action == "query":
            return self.retrieve_context_for_query(
                query=require_str(arguments, "query", action),
                task_type=str(arguments.get("task_type", "auto")),
                max_tokens=int(arguments.get("max_tokens", 1500)),
                limit_entities=int(arguments.get("limit_entities", 1)),
                expand_deps=bool(arguments.get("expand_deps", False)),
                verbosity=str(arguments.get("verbosity", "minimal")),
            )
        if action == "search":
            return self.search_codebase(
                query=require_str(arguments, "query", action),
                limit=int(arguments.get("limit", 10)),
            )
        if action == "semantic_search":
            return self.semantic_search(
                query=require_str(arguments, "query", action),
                limit=int(arguments.get("limit", 10)),
            )
        if action == "context":
            return self.get_entity_context(
                entity_id=require_str(arguments, "entity_id", action),
                task_type=str(arguments.get("task_type", "general")),
                max_tokens=int(arguments.get("max_tokens", 1500)),
            )
        if action == "trace":
            # Resolve names to ids first. ``store.trace_calls`` matches on
            # exact id, so a natural call like entity_id='add' would otherwise
            # return an empty list that reads as "nothing calls this" rather
            # than "that is not an id" — a silently wrong answer.
            requested = require_str(arguments, "entity_id", action)
            resolved = self._resolve_entity_id(requested)
            if resolved is None:
                return {
                    "error": f"Entity not found: {requested}",
                    "code": "entity_not_found",
                    "hint": (
                        "Use action='search' to find the entity, then pass the "
                        "'id' from those results."
                    ),
                }
            traced = self.trace_calls(
                entity_id=resolved,
                direction=str(arguments.get("direction", "callees")),
                depth=int(arguments.get("depth", 1)),
            )
            if resolved != requested:
                return {"entity_id": resolved, "results": traced}
            return traced
        raise ValueError(unknown_action(action, RETRIEVE_ACTIONS))

    def _resolve_entity_id(self, target: str) -> Optional[str]:
        """Map an id, qualified name, or pattern to a concrete entity id."""
        service = self._ensure_service()
        if service.store.get_entity(target) is not None:
            return target
        matches = service.store.search(target)
        return matches[0].id if matches else None

    def run_lifecycle(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a ``knowcode_lifecycle`` action. Submits a background job."""
        # Drop the cached read service *before* the job starts: the artifacts
        # it answers from are about to be superseded, and clearing it now means
        # there is no shared instance for the job to accidentally receive.
        self._invalidate_service()

        # Lifecycle jobs get their own service (see ``_dedicated_service``);
        # ``build`` is the action that creates the store, so it cannot require
        # one to already exist.
        if action == "build":
            job = lifecycle.submit_build(
                jobs=self._jobs,
                root=self.root,
                make_service=self._dedicated_service,
                path=str(arguments.get("path", ".")),
                incremental=bool(arguments.get("incremental", False)),
                temporal=bool(arguments.get("temporal", False)),
                ignore=arguments.get("ignore"),
                on_done=self._invalidate_service,
            )
        elif action == "index":
            job = lifecycle.submit_index(
                jobs=self._jobs,
                root=self.root,
                make_service=self._dedicated_service,
                path=str(arguments.get("path", ".")),
                incremental=bool(arguments.get("incremental", False)),
                on_done=self._invalidate_service,
            )
        elif action == "export":
            job = lifecycle.submit_export(
                jobs=self._jobs,
                root=self.root,
                make_service=self._dedicated_service,
                output=arguments.get("output"),
                on_done=self._invalidate_service,
            )
        else:
            raise ValueError(unknown_action(action, LIFECYCLE_ACTIONS))

        payload = job.to_dict()
        payload["hint"] = (
            "Indexing runs in the background. Poll knowcode_inspect "
            f"action='job_status' job_id='{job.job_id}' until state is "
            "'succeeded' or 'failed'. Do not report success until then."
        )
        return payload

    def inspect(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a ``knowcode_inspect`` action. Read-only."""
        if action == "job_status":
            job_id = arguments.get("job_id")
            return inspection.job_status(self._jobs, str(job_id) if job_id else None)
        if action == "doctor":
            return inspection.doctor(self.root, self.config_path)
        if action == "preflight":
            return inspection.preflight(
                self._service_factory(allow_missing_store=True),
                self.root,
                str(arguments.get("path", ".")),
            )
        if action == "quality":
            return inspection.quality(self._service_factory())
        if action == "stats":
            return inspection.stats(self._service_factory())
        if action == "freshness":
            return inspection.freshness(self._service_factory())
        if action == "telemetry":
            return inspection.telemetry(self.root)
        if action == "history":
            target = arguments.get("target")
            return inspection.history(
                self._service_factory(),
                target=str(target) if target else None,
                limit=int(arguments.get("limit", 10)),
            )
        raise ValueError(unknown_action(action, INSPECT_ACTIONS))

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> str:
        """Handle an MCP tool call.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON string result.
        """
        if not isinstance(arguments, dict):
            return json.dumps({"error": "arguments must be a dictionary"})

        from knowcode.telemetry import query_scope

        # The argument payload is entirely client-supplied and routinely holds
        # the user's question and pasted code, so it is never logged (Step 20).
        # A query-bearing call opens the scope here so the tool event and the
        # retrieval it triggers share one correlation id and one counted query.
        query = arguments.get("query")
        if isinstance(query, str):
            with query_scope(self.store_path, query=query, entry_point="mcp"):
                return self._dispatch_tool_call(name, arguments, len(query))
        return self._dispatch_tool_call(name, arguments, None)

    @staticmethod
    def _payload_outcome(payload: str) -> str:
        """Classify a tool result as ``ok`` or ``error`` without reading it."""
        try:
            parsed = json.loads(payload)
        except ValueError:  # pragma: no cover - every branch emits JSON
            return "error"
        return "error" if isinstance(parsed, dict) and "error" in parsed else "ok"

    def _log_tool_call(
        self,
        name: str,
        argument_count: int,
        query_chars: Optional[int],
        payload: str,
        action: Optional[str] = None,
    ) -> None:
        """Record the call's shape: which tool and action, argument count, outcome."""
        from knowcode.telemetry import current_query_id, log_event

        event: dict[str, Any] = {
            "event_type": "tool_call",
            "tool_name": name,
            "argument_count": argument_count,
            "outcome": self._payload_outcome(payload),
        }
        # With a consolidated surface, ``tool_name`` no longer identifies the
        # capability used, so per-action counts need the action too. Action
        # names are a closed enum defined in this package, never user content,
        # so recording one cannot leak a question or pasted code.
        if action is not None:
            event["action"] = action
        if query_chars is not None:
            event["query_id"] = current_query_id()
            event["query_chars"] = query_chars
        try:
            log_event(self.store_path, event)
        except OSError as e:
            import logging

            logging.getLogger(__name__).warning("Ignored telemetry OS error: %s", e)

    def _dispatch_tool_call(
        self, name: str, arguments: dict[str, Any], query_chars: Optional[int]
    ) -> str:
        """Route one tool call and record it, whatever its outcome."""
        payload = self._run_tool(name, arguments)
        raw_action = arguments.get("action")
        action = raw_action if isinstance(raw_action, str) else None
        self._log_tool_call(name, len(arguments), query_chars, payload, action)
        return payload

    @staticmethod
    def _mcp_hint(error: KnowCodePrerequisiteError) -> str:
        """Rewrite a CLI-shaped hint into one an MCP agent can act on.

        The typed errors are shared with the CLI, so their hints say things
        like "Run `knowcode build <dir>` first". An agent reached through MCP
        has no terminal; the equivalent move is a tool call.
        """
        if error.code in {"missing_knowledge_store", "missing_semantic_index"}:
            return (
                "Call knowcode_lifecycle with action='build' to create the "
                "artifacts for this repository, poll knowcode_inspect "
                "action='job_status' until it succeeds, then retry."
            )
        return error.hint

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one tool and return its JSON payload."""
        try:
            result: dict[str, Any] | list[dict[str, Any]]

            # Consolidated surface: one tool per concern, action selects the
            # capability. This is the default; the deprecated flat tools are
            # handled in knowcode.mcp.legacy, kept for one release.
            if name in _CONSOLIDATED_ROUTES:
                action = arguments.get("action")
                if not isinstance(action, str) or not action:
                    raise ValueError(f"{name} requires 'action' as a string")
                result = _CONSOLIDATED_ROUTES[name](self, action, arguments)
            else:
                legacy = dispatch_legacy(self, name, arguments)
                if legacy is UNHANDLED:
                    result = {"error": f"Unknown tool: {name}"}
                else:
                    result = legacy

            return json.dumps(result, separators=(",", ":"))
        except KnowCodePrerequisiteError as e:
            return json.dumps(
                {"error": str(e), "code": e.code, "hint": self._mcp_hint(e)},
                separators=(",", ":"),
            )
        except ValueError as e:
            return json.dumps(
                {"error": f"Validation Error: {e}"}, separators=(",", ":")
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, separators=(",", ":"))


#: Consolidated tool name -> bound router. Defined after the class so the
#: methods exist; keyed by tool name so ``_run_tool`` stays a flat dispatch.
_CONSOLIDATED_ROUTES: dict[
    str, Callable[[KnowCodeMCPServer, str, dict[str, Any]], Any]
] = {
    "knowcode_retrieve": KnowCodeMCPServer.retrieve,
    "knowcode_lifecycle": KnowCodeMCPServer.run_lifecycle,
    "knowcode_inspect": KnowCodeMCPServer.inspect,
}


def create_server(
    store_path: str | Path,
    config_path: Optional[str] = None,
    include_legacy_tools: bool = False,
) -> "Server":
    """Create an MCP server instance.

    Args:
        store_path: Repository root, or a path to a knowledge store file.
        config_path: Optional configuration file path for model priorities.
        include_legacy_tools: Also advertise the five deprecated flat tools.
            Off by default: advertising both surfaces pays both schema costs
            on every turn, which is the cost consolidation exists to remove.

    Returns:
        Configured MCP Server.
    """
    if not MCP_AVAILABLE:
        raise ImportError("MCP package not installed. Install with: pip install mcp")

    server = Server("knowcode")
    knowcode = KnowCodeMCPServer(store_path, config_path=config_path)
    definitions = tool_definitions(include_legacy=include_legacy_tools)

    @server.list_tools()  # type: ignore
    async def list_tools() -> list[Tool]:
        """List available KnowCode tools."""
        return [_build_tool(definition) for definition in definitions]

    @server.call_tool()  # type: ignore
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a KnowCode tool."""
        result_text = knowcode.handle_tool_call(name, arguments)
        # ``isError`` lets the client distinguish a failed call from a
        # successful one carrying an error-shaped body; without it, a missing
        # store reads to the client as a normal result.
        return CallToolResult(
            content=[TextContent(type="text", text=result_text)],
            isError=is_error_payload(result_text),
        )

    return server


def _build_tool(definition: dict[str, Any]) -> "Tool":
    """Construct an MCP ``Tool``, carrying annotations and ``_meta`` through.

    Standard hints (``readOnlyHint`` and friends) are advertised for clients
    that use them. The lifecycle tool additionally sets
    ``anthropic/requiresUserInteraction`` via ``_meta``, because that is the
    annotation Claude Code documents as forcing per-call approval, and a build
    both spends embedding quota and sends repository text off the machine.
    """
    from mcp.types import ToolAnnotations

    kwargs: dict[str, Any] = {
        "name": definition["name"],
        "description": definition["description"],
        "inputSchema": definition["inputSchema"],
    }
    if definition.get("title"):
        kwargs["title"] = definition["title"]
    if definition.get("annotations"):
        kwargs["annotations"] = ToolAnnotations(**definition["annotations"])
    if definition.get("_meta"):
        kwargs["_meta"] = definition["_meta"]
    return Tool(**kwargs)


async def run_server_async(
    store_path: str | Path,
    config_path: Optional[str] = None,
    include_legacy_tools: bool = False,
) -> None:
    """Run the MCP server with STDIO transport.

    Args:
        store_path: Repository root, or a path to a knowledge store file.
        config_path: Optional configuration file path for model priorities.
        include_legacy_tools: Also advertise the five deprecated flat tools.
    """
    if not MCP_AVAILABLE:
        raise ImportError("MCP package not installed. Install with: pip install mcp")

    server = create_server(
        store_path,
        config_path=config_path,
        include_legacy_tools=include_legacy_tools,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_server(
    store_path: str | Path,
    config_path: Optional[str] = None,
    include_legacy_tools: bool = False,
) -> None:
    """Run the MCP server (blocking).

    Args:
        store_path: Repository root, or a path to a knowledge store file.
        config_path: Optional configuration file path for model priorities.
        include_legacy_tools: Also advertise the five deprecated flat tools.
    """
    asyncio.run(
        run_server_async(
            store_path,
            config_path=config_path,
            include_legacy_tools=include_legacy_tools,
        )
    )
