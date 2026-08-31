# IDE & Agent Integration

How to connect an MCP-capable IDE or agent to KnowCode, and how to wrap the
REST API as LLM function-calling tools for frameworks that cannot use MCP.
The canonical agent retrieval policy is the
[MCP contract](../mcp-contract.md) — thresholds, verbosity rules, and token
budgets live there so every agent behaves the same way; don't hard-code
separate values per client.

## Why MCP

Agents retrieve focused repository context *before* building a large
prompt. The low-token workflow:

1. Agent receives a user query.
2. Agent calls `knowcode_retrieve` with `action="query"` and
   `verbosity="minimal"`.
3. KnowCode returns compact `context_text`, `sufficiency_score`, and token
   count.
4. Agent answers locally when the configured threshold is met
   (`sufficiency_threshold`, default 0.8).
5. Agent escalates budget or verbosity only when minimal context is not
   enough.

## Prerequisites

- KnowCode installed (with the `mcp` extra: `pip install "knowcode[mcp]"`).
- A built knowledge store and index:

```bash
knowcode build .
knowcode doctor --store . --mcp
```

## Client configuration

The server resolves its repository root from, in order: an explicit
`--store`, the `CLAUDE_PROJECT_DIR` environment variable, then the process
working directory. Claude Code does not document the working directory it
spawns a stdio server with and its own guidance is to resolve
project-relative paths from `CLAUDE_PROJECT_DIR`, so **do not rely on
`--store .`** — omit `--store` and let the env var win.

That makes one registration serve every repository:

```bash
claude mcp add knowcode --scope user -- knowcode mcp-server
```

Which is equivalent to:

```json
{
  "mcpServers": {
    "knowcode": {
      "command": "knowcode",
      "args": ["mcp-server"]
    }
  }
}
```

Pin a single repository instead by passing an absolute `--store`:

```json
{
  "mcpServers": {
    "knowcode": {
      "command": "/absolute/path/to/repository/.venv/bin/knowcode",
      "args": ["mcp-server", "--store", "/absolute/path/to/repository"]
    }
  }
}
```

## Running from a checkout

A server launched with `uvx --from <checkout>[mcp]` does **not** pick up edits
in the checkout: uv resolves that requirement onto a cached tool environment
(`~/.local/share/uv/tools/knowcode`) and reuses it indefinitely for a path
source, and `--refresh` does not rebuild it. The server will happily run
month-old code against a fresh store with no signal of the drift
([BL-32](../engineering/backlog.md)). Two launch shapes avoid the trap:

- The repository's own venv, which always executes the working tree:
  `bin/knowcode-mcp.sh mcp-server --store /absolute/path/to/repository`.
- A reinstall after each pull — the cache clean is load-bearing, `--force`
  alone reinstalls from the cached wheel and reproduces the staleness:
  `uv cache clean knowcode && uv tool install --force --python 3.11 '<checkout>[mcp]'`.

Restart the IDE or agent host after changing its MCP config. On macOS, if
the stdio server is not spawned correctly from a GUI client, the wrapper
script `bin/knowcode-mcp.sh` works around provenance checks.

### Bootstrapping a new repository

The server starts whether or not artifacts exist, so an agent in a
repository KnowCode has never seen can build them itself: call
`knowcode_lifecycle` with `action="build"`, then poll `knowcode_inspect`
`action="job_status"`. No terminal step is required.

### Permissions

Tools are named `mcp__knowcode__<tool>`. Because the surface is split by
concern, retrieval can be allowlisted without also allowing builds:

```json
{
  "permissions": {
    "allow": ["mcp__knowcode__knowcode_retrieve", "mcp__knowcode__knowcode_inspect"]
  }
}
```

`knowcode_lifecycle` declares `anthropic/requiresUserInteraction`, so every
build is confirmed even if the server is otherwise allowlisted. A build
sends repository chunk text to the configured embedding provider and consumes
quota, so this is deliberate.

### Long builds

Lifecycle actions return a `job_id` immediately rather than blocking, because
indexing costs one embedding round-trip per file with no cross-file batching
or concurrency — a few hundred files is minutes, a few thousand is tens of
minutes. Claude Code's stdio idle timeout defaults to 30 minutes
(`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`), which a synchronous build could
exceed. Polling is cheap and read-only.

## Tools

The server exposes three consolidated tools; an `action` selects the
capability. The canonical policy, token budgets, and escalation ladder live in
the [MCP contract](../mcp-contract.md).

| Tool | Actions |
|---|---|
| `knowcode_retrieve` | `query` (primary), `search`, `context`, `trace`, `semantic_search` |
| `knowcode_lifecycle` | `build`, `index`, `export` |
| `knowcode_inspect` | `job_status`, `doctor`, `freshness`, `quality`, `stats`, `preflight`, `history`, `telemetry` |

Call `knowcode_retrieve` with `action="query"` first for ordinary repository
questions; use the rest for focused follow-up.

Deliberately excluded: telemetry deletion, daemon/host control
(`server`, `mcp-server`, `install`), and `ask` — inside an agent the model
should consume retrieved context rather than pay a second LLM.

The five original flat tools remain available for one release via
`--legacy-tools` (off by default).

## Agent rule snippet

Put only a pointer plus the compact rule in agent-specific config files:

```md
When repository context is needed, follow docs/mcp-contract.md.
Start with knowcode_retrieve action=query, verbosity=minimal, and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured sufficiency_threshold from
aimodels.yaml to decide whether to answer from local context.
```

## Verification

```bash
knowcode doctor --store . --mcp
```

The MCP handshake check spawns the server, lists tools, and calls the primary
tool, so a green result proves the surface really answers.

Then ask the agent a repository question such as *“Where is the retrieval
orchestration implemented?”* Expected: the agent calls `knowcode_retrieve`
with `action="query"` and `verbosity="minimal"` first, answers locally when
`sufficiency_score` meets the threshold, and escalates only if
insufficient. A manual test plan with expected sufficiency scores per
question is in `tests/test_mcp_workflow.md`.

Target baselines to aim for (actuals depend on your codebase and queries):
70%+ of queries with `sufficiency_score ≥ 0.8`, and a 50%+ reduction in
external LLM token usage. Monitor `knowcode telemetry show` against these
targets.

## Troubleshooting

**MCP server not available.** Check the command path, that `--store` is an
absolute path, and that the IDE was restarted. Run the server directly to
see errors: `knowcode mcp-server --store .`

**Knowledge store not found.** Call `knowcode_lifecycle action="build"`
(or run `knowcode build .`) and poll `job_status` until it succeeds. Note
that a current build publishes the graph as `knowledge.db` *inside*
`knowcode_index/generations/`; the flat `knowcode_knowledge.json` is a legacy
export written only with `export_json`. Both layouts count as ready, so do
not expect the JSON to exist.

**Semantic retrieval fallback.** Rebuild the index (`knowcode index .`) and
confirm embedding keys from `aimodels.yaml` are present in the *agent's*
environment.

**Stale results (`is_stale: true`).** Run `knowcode build .` to refresh
store and index together; with the server running, `POST /api/v1/reload`.

**Context still too small.** Follow the ladder in the
[MCP contract](../mcp-contract.md): increase breadth while staying in
`minimal`, then `standard` for implementation detail, then `verbose` for
evidence. Don't make `diagnostic` the default.

## Security notes

- The MCP server runs locally over stdio; it never exposes a port.
- `knowcode_knowledge.json` and `knowcode_index/` contain repository-derived
  data — treat them like your source.
- Embedding providers receive chunk text during indexing, depending on your
  configured provider.
- Store API keys in environment variables, not committed files.

## Non-MCP agent frameworks (OpenAPI function calling)

For custom agent frameworks that cannot use MCP, translate KnowCode's REST
API into native LLM tools: the FastAPI server publishes its schema at
`/openapi.json`, which you map to tool definitions.

1. **Start the server:** `knowcode server --port 8000` — the spec is served
   at `http://127.0.0.1:8000/openapi.json`.
2. **Translate endpoints to tools.** Frameworks like LangChain's
   `RequestsToolkit` or LlamaIndex's `OpenAPIToolSpec` automate this; by
   hand:

```python
import requests

openapi_spec = requests.get("http://127.0.0.1:8000/openapi.json").json()

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_context",
            "description": "Execute semantic search and return relevant code chunks with context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "task_type": {"type": "string",
                                  "enum": ["explain", "debug", "extend", "review", "locate", "general"]}
                },
                "required": ["query"]
            }
        }
    }
    # ... repeat for the endpoints below
]
```

3. **Execute tool calls** by hitting the corresponding HTTP endpoint and
   feeding the response back as the tool result message.

The highest-value endpoints to expose as tools:

| Tool | Endpoint | Role |
|---|---|---|
| `query_context` | `POST /api/v1/context/query` | Primary discovery — natural-language semantic search |
| `search` | `GET /api/v1/search?q=` | Exact symbol lookup |
| `get_context` | `GET /api/v1/context?target=` | Deep dive on a discovered entity |
| `trace_calls` | `GET /api/v1/trace_calls/{entity_id}` | Caller/callee dependency mapping |

The full endpoint reference, including rate limits, is in
[REST API](rest-api.md).
