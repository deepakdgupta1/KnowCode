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
2. Agent calls `retrieve_context_for_query` with `verbosity="minimal"`.
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

Use an absolute repository path for `--store`. In a development checkout,
`uv run` keeps the MCP server on the project's environment.

```json
{
  "mcpServers": {
    "knowcode": {
      "command": "uv",
      "args": ["run", "knowcode", "mcp-server", "--store", "/absolute/path/to/repository"]
    }
  }
}
```

If your client cannot run through `uv`, point `command` at the absolute
`knowcode` executable:

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

Restart the IDE or agent host after changing its MCP config. On macOS, if
the stdio server is not spawned correctly from a GUI client, the wrapper
script `bin/knowcode-mcp.sh` works around provenance checks.

## Tools

The MCP server exposes five read-only, deterministic tools:

| Tool | Use |
|---|---|
| `retrieve_context_for_query` | **Primary** natural-language retrieval path |
| `search_codebase` | Find entities by known name or pattern |
| `get_entity_context` | Context for a specific known entity |
| `trace_calls` | Callers/callees for a specific entity |
| `assess_codebase_quality` | The preflight report card over MCP |

Call `retrieve_context_for_query` first for ordinary repository questions;
use the others for focused follow-up once retrieval has identified the
relevant entity.

## Agent rule snippet

Put only a pointer plus the compact rule in agent-specific config files:

```md
When repository context is needed, follow docs/mcp-contract.md.
Start with retrieve_context_for_query using verbosity=minimal and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured sufficiency_threshold from
aimodels.yaml to decide whether to answer from local context.
```

## Verification

```bash
knowcode doctor --store . --mcp
```

Then ask the agent a repository question such as *“Where is the retrieval
orchestration implemented?”* Expected: the agent calls
`retrieve_context_for_query` with `verbosity="minimal"` first, answers
locally when `sufficiency_score` meets the threshold, and escalates only if
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

**Knowledge store not found.** Run `knowcode build .`, then confirm the
config's `--store` points at the directory containing
`knowcode_knowledge.json`.

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
