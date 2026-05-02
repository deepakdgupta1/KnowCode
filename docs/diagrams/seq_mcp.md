# Sequence Diagram — MCP Server Workflow

> Textual narration of [`seq_mcp.drawio`](seq_mcp.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Triggered by:** `knowcode mcp-server`
**Transport:** STDIO / JSON-RPC 2.0
**Clients:** Claude Desktop, VS Code, JetBrains, any MCP-compatible IDE

---

## Participants

| Participant | File | Role |
|---|---|---|
| IDE / Claude Desktop | — | MCP client — sends `tools/call` JSON-RPC requests |
| KnowCodeMCPServer | `mcp/server.py` | MCP server — routes tool calls, formats results |
| KnowCodeService | `service.py` | Central orchestrator — performs all actual work |
| KnowledgeStore | `storage/knowledge_store.py` | In-memory knowledge graph (entity/relationship data) |
| ContextSynthesizer | `analysis/context_synthesizer.py` | Builds task-prioritized context bundles |
| RetrievalOrchestrator | `retrieval/orchestrator.py` | Full hybrid retrieval pipeline (Tool 4 only) |

---

## Startup

### Step 1 — Launch MCP server

```
User → KnowCodeMCPServer:  knowcode mcp-server
```

### Step 2 — Start async runtime

```
KnowCodeMCPServer:  run_server()  →  asyncio.run(run_server_async())
```

### Step 3 — Open STDIO transport

```
KnowCodeMCPServer:  stdio_server(KnowCodeMCPServer)
                    →  STDIO transport  (stdin/stdout pipes)
```

### Step 4 — MCP initialize handshake

```
IDE / Claude Desktop → KnowCodeMCPServer:
  MCP initialize  (JSON-RPC 2.0)
```

### Step 5 — Advertise tools

```
KnowCodeMCPServer → IDE / Claude Desktop:
  tools/list response  →  4 tools with full JSON schemas
```

### Step 6 — Lazy service initialization

```
KnowCodeMCPServer → KnowCodeService:
  KnowCodeService(store_path, strict_config=False)
  [initialized on the first tool call, not at startup]
```

---

## Tool 1 — `search_codebase`

**Signature:** `search_codebase(query: str, limit: int = 10)`

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "search_codebase", arguments: {query, limit}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:  service.search(query)
KnowCodeService   → KnowledgeStore:   knowledge_store.search(query)
```

`knowledge_store.search()` uses substring and token matching on entity `name` and `qualified_name` fields.

### Response

```
KnowledgeStore → IDE:
  [{id, name, qualified_name, kind, file_path, line_start}]  top limit results
```

---

## Tool 2 — `get_entity_context`

**Signature:** `get_entity_context(entity_id: str, task_type: str = "general", max_tokens: int = 2000)`

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "get_entity_context", arguments: {entity_id, task_type, max_tokens}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.get_context(entity_id, task_type, max_tokens)

KnowCodeService → KnowledgeStore:
  entity = store.get_entity(entity_id)  [fallback to store.search() if not found by ID]

KnowCodeService → ContextSynthesizer:
  synthesizer.synthesize_with_task(entity_id, task_type)
  →  applies TASK_TEMPLATES priority order + per-section boost multipliers
```

ContextSynthesizer fetches related nodes from KnowledgeStore:
- `parent` entity
- `callers[]` (entities that call this one)
- `callees[]` (entities this one calls)
- `children[]` (nested entities)

```
ContextSynthesizer:
  _calculate_sufficiency(task_type, content_included, entity, text)  →  float 0.0–1.0
```

### Response

```
ContextSynthesizer → IDE:
  {entity_id, qualified_name, context_text, total_tokens, sufficiency_score, task_type}
```

---

## Tool 3 — `trace_calls`

**Signature:** `trace_calls(entity_id: str, direction: str = "callees", depth: int = 1)`

Valid direction values: `callers` | `callees`. Valid depth range: 1–5.

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {name: "trace_calls", arguments: {entity_id, direction, depth}}
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.store.trace_calls(entity_id, direction, depth, max_results=50)
```

```
KnowledgeStore:
  BFS traversal on relationship graph
  (CALLS / IMPORTED_BY edges, up to `depth` levels, max_results=50 nodes)
```

### Response

```
KnowledgeStore → IDE:
  [{id, name, qualified_name, kind, file_path, line_start, call_depth}]
```

---

## Tool 4 — `retrieve_context_for_query`

**Signature:**
```
retrieve_context_for_query(
  query: str,
  task_type: str = "auto",
  max_tokens: int = 6000,
  limit_entities: int = 3,
  expand_deps: bool = True,
  verbosity: str = "minimal"
)
```

### Invocation

```
IDE → KnowCodeMCPServer:
  tools/call  {
    name: "retrieve_context_for_query",
    arguments: {query, task_type, max_tokens, limit_entities, expand_deps, verbosity}
  }
```

### Execution

```
KnowCodeMCPServer → KnowCodeService:
  service.retrieve_context_for_query(…)

KnowCodeService → RetrievalOrchestrator:
  full hybrid pipeline:
  classify → embed → BM25+FAISS → rerank → expand_dependencies → synthesize
```

> This is the same pipeline described in `seq_query_retrieval.drawio` — steps 4 through 14 apply in full.

```
RetrievalOrchestrator → KnowCodeMCPServer:
  {context_text, sufficiency_score, total_tokens,
   [+ query, task_type, retrieval_mode, evidence[]  per verbosity level]}
```

### Result formatting

```
KnowCodeMCPServer:
  format_result()
  →  MCP content block  {type: "text", text: json.dumps(result)}

KnowCodeMCPServer → IDE:
  tools/call response
```

---

## Error Handling

All tool handler exceptions are caught at the server level. On error the server returns:

```json
{
  "isError": true,
  "content": [{"type": "text", "text": "<error_message>"}]
}
```

No unhandled exception propagates through the STDIO transport.

---

## Tool Summary

| Tool | Arguments | Internal call | Returns |
|---|---|---|---|
| `search_codebase` | `query`, `limit=10` | `knowledge_store.search()` — substring + token match | `[{id, name, qualified_name, kind, file_path, line_start}]` top limit |
| `get_entity_context` | `entity_id`, `task_type=general`, `max_tokens=2000` | `synthesize_with_task()` + `_calculate_sufficiency()` | `{entity_id, qualified_name, context_text, total_tokens, sufficiency_score, task_type}` |
| `trace_calls` | `entity_id`, `direction=callees`, `depth=1` | BFS on relationship graph (max\_results=50) | `[{id, name, qualified_name, kind, file_path, line_start, call_depth}]` |
| `retrieve_context_for_query` | `query`, `task_type=auto`, `max_tokens=6000`, `limit_entities=3`, `expand_deps=true`, `verbosity=minimal` | Full hybrid pipeline (steps 4–14 of seq\_query\_retrieval) | `{context_text, sufficiency_score, total_tokens, …per verbosity}` |
