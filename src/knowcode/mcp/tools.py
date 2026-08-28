"""MCP tool definitions for the KnowCode surface.

The surface is three consolidated tools, each taking an ``action`` enum,
rather than one tool per capability. Tool schemas are injected into *every*
LLM request by the client, so schema size is a recurring per-turn tax.

Measured, at ~4 chars per token:

===================  =====  ============  ======
Surface              Tools  Capabilities  Tokens
===================  =====  ============  ======
Previous (flat)          5             5    ~650
This one                 3            14  ~1,110
Flat parity (14)        14            14  ~2,000
===================  =====  ============  ======

So this is not a reduction against the old surface — it is 1.7x the schema
for 2.8x the capabilities, and about half what one-tool-per-capability would
cost. ``tests/unit/mcp/test_consolidated_surface.py`` holds the ceiling so
adding a capability is a deliberate decision. This is the consolidated
surface roadmap P3 calls for.

The split is by *concern*, not alphabetically, because client permissions are
per-tool (``mcp__knowcode__<tool>``):

- ``knowcode_retrieve`` — read-only, the hot path. Safe to allowlist so
  ordinary repository questions never prompt.
- ``knowcode_lifecycle`` — builds artifacts, spends embedding-provider
  quota, and sends chunk text off the machine. Carries
  ``anthropic/requiresUserInteraction`` so every call is confirmed.
- ``knowcode_inspect`` — read-only diagnostics, including polling a
  lifecycle job. Polling lives here precisely *because* it must not inherit
  the lifecycle tool's per-call confirmation.
"""

from __future__ import annotations

from typing import Any

#: Client-honored annotation that forces explicit approval on every call.
#: Standard MCP hints (``readOnlyHint`` and friends) are set too, but are not
#: documented as affecting Claude Code's permission flow, so the tool that
#: spends money and uploads source does not rely on them alone.
REQUIRES_USER_INTERACTION = "anthropic/requiresUserInteraction"

TASK_TYPES = ["auto", "explain", "debug", "extend", "review", "locate", "general"]
VERBOSITY_LEVELS = ["minimal", "standard", "verbose", "diagnostic"]

RETRIEVE_ACTIONS = ["query", "search", "context", "trace", "semantic_search"]
# ``analyze`` is deliberately absent: the CLI's ``analyze`` and ``build`` both
# call ``service.analyze()`` with the same arguments, so exposing both would
# cost tokens and invite the agent to pick between identical behaviours.
LIFECYCLE_ACTIONS = ["build", "index", "export"]
INSPECT_ACTIONS = [
    "doctor",
    "preflight",
    "quality",
    "stats",
    "freshness",
    "history",
    "telemetry",
    "job_status",
]


RETRIEVE_TOOL: dict[str, Any] = {
    "name": "knowcode_retrieve",
    "title": "KnowCode: retrieve repository context",
    "description": (
        "Token-efficient context about this repository from KnowCode's semantic "
        "graph. Use action='query' for any natural-language question: it returns "
        "context_text plus a sufficiency_score, so you can answer without reading "
        "files. Start minimal and escalate max_tokens/verbosity only if the "
        "context proved insufficient.\n"
        "query: question -> context bundle (default choice). search: find entities "
        "by name. context: detail for a known entity_id. trace: callers/callees. "
        "semantic_search: raw ranked chunks.\n"
        "Read-only; never builds. Missing artifacts return "
        "code='missing_knowledge_store' — run knowcode_lifecycle build, then retry."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": RETRIEVE_ACTIONS, "default": "query"},
            "query": {
                "type": "string",
                "description": "Question (query) or name pattern (search, semantic_search).",
            },
            "entity_id": {
                "type": "string",
                "description": "Entity ID or qualified name (context, trace).",
            },
            "task_type": {"type": "string", "enum": TASK_TYPES, "default": "auto"},
            "max_tokens": {
                "type": "integer",
                "description": "Budget: 1500 explain/locate, 2000 debug, 3000 review.",
                "default": 1500,
            },
            "limit_entities": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
                "maximum": 10,
            },
            "expand_deps": {
                "type": "boolean",
                "description": "Include call-graph neighbours.",
                "default": False,
            },
            "verbosity": {
                "type": "string",
                "enum": VERBOSITY_LEVELS,
                "description": "Keep 'minimal' unless it was insufficient.",
                "default": "minimal",
            },
            "direction": {
                "type": "string",
                "enum": ["callers", "callees"],
                "default": "callees",
            },
            "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 5},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["action"],
    },
}


LIFECYCLE_TOOL: dict[str, Any] = {
    "name": "knowcode_lifecycle",
    "title": "KnowCode: build repository artifacts",
    "description": (
        "Build or refresh KnowCode's artifacts. Run once in a repository that has "
        "never been indexed, and again when results report stale.\n"
        "build: full pipeline (usual choice). index: semantic index only. "
        "export: write Markdown docs.\n"
        "Returns a job_id immediately and runs in the background; a cold build "
        "takes many minutes. Poll knowcode_inspect job_status until state is "
        "'succeeded' or 'failed' — do not assume success. One job at a time.\n"
        "COST: sends repository text to the configured embedding provider and "
        "consumes quota. Paths are confined to the repository root."
    ),
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        # Indexing calls an external embedding provider.
        "openWorldHint": True,
    },
    "_meta": {REQUIRES_USER_INTERACTION: True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": LIFECYCLE_ACTIONS, "default": "build"},
            "path": {
                "type": "string",
                "description": "Directory to process, relative to the repository root.",
                "default": ".",
            },
            "output": {"type": "string", "description": "Output directory (export)."},
            "incremental": {
                "type": "boolean",
                "description": "Reuse unchanged embeddings; much cheaper on rebuild.",
                "default": False,
            },
            "temporal": {
                "type": "boolean",
                "description": "Also analyze git history. Much slower.",
                "default": False,
            },
            "ignore": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["action"],
    },
}


INSPECT_TOOL: dict[str, Any] = {
    "name": "knowcode_inspect",
    "title": "KnowCode: inspect state and diagnostics",
    "description": (
        "KnowCode's own state for this repository: readiness, freshness, and how "
        "much to trust its output. Read-only and cheap.\n"
        "job_status: poll a lifecycle job (job_id, or omit for the latest). "
        "freshness: are artifacts stale vs the working tree. quality: persisted "
        "report card. doctor: readiness. stats: counts. preflight: ad-hoc "
        "assessment (parses the tree, slower). history: commits or entity "
        "revisions. telemetry: local usage summary.\n"
        "Check freshness or quality when deciding whether to trust context."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": INSPECT_ACTIONS},
            "job_id": {"type": "string"},
            "target": {
                "type": "string",
                "description": "Entity ID or pattern (history); omit for commit log.",
            },
            "path": {"type": "string", "default": "."},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["action"],
    },
}


#: The default surface.
CONSOLIDATED_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    RETRIEVE_TOOL,
    LIFECYCLE_TOOL,
    INSPECT_TOOL,
]

#: Canonical primary tool. ``doctor --mcp`` asserts this one is listed.
PRIMARY_TOOL_NAME = "knowcode_retrieve"


# ----------------------------------------------------------------------
# Legacy surface
# ----------------------------------------------------------------------
# The five original flat tools, kept reachable behind an explicit opt-in for
# one release so existing client configs and agent rules keep working during
# migration (roadmap P3). They are OFF by default: leaving them on would pay
# both schema costs on every turn, which is the outcome consolidation exists
# to avoid.

LEGACY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_codebase",
        "description": "DEPRECATED — use knowcode_retrieve action='search'. Search for code entities by name or pattern.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search pattern"},
                "limit": {
                    "type": "integer",
                    "description": "Max results",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_entity_context",
        "description": "DEPRECATED — use knowcode_retrieve action='context'. Detailed context for one entity.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Entity ID or qualified name",
                },
                "task_type": {
                    "type": "string",
                    "enum": [
                        "explain",
                        "debug",
                        "extend",
                        "review",
                        "locate",
                        "general",
                    ],
                    "default": "general",
                },
                "max_tokens": {"type": "integer", "default": 2000},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "trace_calls",
        "description": "DEPRECATED — use knowcode_retrieve action='trace'. Trace callers or callees.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Starting entity ID"},
                "direction": {
                    "type": "string",
                    "enum": ["callers", "callees"],
                    "default": "callees",
                },
                "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 5},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "retrieve_context_for_query",
        "description": "DEPRECATED — use knowcode_retrieve action='query'. Token-budgeted context bundle for a natural-language query.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query"},
                "task_type": {"type": "string", "enum": TASK_TYPES, "default": "auto"},
                "max_tokens": {"type": "integer", "default": 4000},
                "limit_entities": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10,
                },
                "expand_deps": {"type": "boolean", "default": True},
                "verbosity": {
                    "type": "string",
                    "enum": VERBOSITY_LEVELS,
                    "default": "minimal",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "assess_codebase_quality",
        "description": "DEPRECATED — use knowcode_inspect action='quality'. Pre-flight quality report card.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def tool_definitions(include_legacy: bool = False) -> list[dict[str, Any]]:
    """Return the tool definitions to advertise.

    Args:
        include_legacy: Also advertise the five deprecated flat tools.

    Returns:
        Tool definition dicts, consolidated surface first.
    """
    if include_legacy:
        return [*CONSOLIDATED_TOOL_DEFINITIONS, *LEGACY_TOOL_DEFINITIONS]
    return list(CONSOLIDATED_TOOL_DEFINITIONS)


#: Back-compat alias. ``TOOL_DEFINITIONS`` was the module-level registry that
#: tests and ``doctor`` imported from ``knowcode.mcp.server``; it now names the
#: default surface.
TOOL_DEFINITIONS = CONSOLIDATED_TOOL_DEFINITIONS
