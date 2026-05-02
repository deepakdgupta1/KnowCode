# Sequence Diagram — Agent Gateway Workflow

> Textual narration of [`seq_agent_gateway.drawio`](seq_agent_gateway.drawio).
> Every participant, message, and note in the draw.io file is described here in full.

**Located in:** `apps/agent-gateway/`
**Startup:** `local_up.sh`
**Request entry:** `POST /api/v1/chat`
**Smoke test:** `scripts/smoke_e2e.py`

---

## Participants

| Participant | File | Role |
|---|---|---|
| User / IDE | — | Sends chat requests to the Gateway |
| Gateway FastAPI `:8081` | `app.py` | HTTP server — validates requests, delegates to orchestrator |
| AgentOrchestrator | `orchestrator.py` | Agentic tool-use loop (max 4 rounds) |
| ToolSelector | `tool_selector.py` | Keyword heuristics — selects tool subset from user message |
| OpenAPIToolRegistry | `openapi_tools.py` | Caches OpenAI-compatible tool schemas derived from KnowCode OpenAPI spec |
| LiteLLMClient | `litellm_client.py` | Sends chat completion requests to LiteLLM proxy |
| LiteLLM Proxy `:4000` | external | Normalizes to upstream LLMs (Gemini, Mistral, …) |
| KnowCodeClient | `knowcode_client.py` | Dispatches tool calls to KnowCode REST API |
| KnowCode REST API `:8000` | `src/knowcode/api/api.py` | The main KnowCode service API |

---

## Startup — `local_up.sh`

### Step 1 — Start dependencies

```
local_up.sh:
  → start KnowCode REST API on :8000
  → start LiteLLM proxy on :4000
```

### Step 2 — Load settings

```
Gateway:  GatewaySettings.from_env()
```

Settings loaded (frozen dataclass, all from environment variables):

| Setting | Default |
|---|---|
| `knowcode_api_base_url` | — (required) |
| `litellm_base_url` | — (required) |
| `default_model` | — (required) |
| `max_tool_rounds` | `4` |
| `tool_timeout_seconds` | `30` |
| `openapi_cache_ttl_seconds` | `300` |

### Step 3 — Fetch OpenAPI spec

```
Gateway → KnowCode REST API:
  GET  {knowcode_api_base_url}/openapi.json
```

```
KnowCode REST API → OpenAPIToolRegistry:  OpenAPI spec JSON
```

### Step 4 — Translate to tool schemas

```
OpenAPIToolRegistry:
  OpenAPIToolTranslator.translate(openapi_spec)
  →  OpenAI-compatible tool schema list  (cached for 300 s)
```

### Step 5 — Gateway ready

```
Gateway:  listening on :8081
```

---

## Agentic Request — `POST /api/v1/chat`

### Step 6 — Receive chat request

```
User / IDE → Gateway:
  POST /api/v1/chat
  ChatRequest{
    message,
    conversation[],
    model,
    tags,
    tool_names,
    temperature
  }
```

### Step 7 — Delegate to orchestrator

```
Gateway → AgentOrchestrator:  orchestrator.run(chat_request)
```

### Step 8 — Select tools

```
AgentOrchestrator → ToolSelector:
  _pick_tool_names(request)  →  select_tool_names(message)
```

Keyword heuristics (not ML):

| Keyword pattern | Tool selected |
|---|---|
| `explain`, `what is`, `describe` | `get_context` |
| `find`, `search`, `where` | `search` |
| `trace`, `who calls`, `callers` | `trace_calls` |
| (default) | all four tools |

Returns: subset of `{query_context, search, get_context, trace_calls}`.

### Step 9 — Fetch tool schemas

```
AgentOrchestrator → OpenAPIToolRegistry:
  get tool schemas for selected tools
```

Returns: list of OpenAI-compatible tool schema dicts.

---

## Tool-Use Loop — up to `max_tool_rounds=4` iterations

### Step 10 — LLM completion with tools

```
AgentOrchestrator → LiteLLMClient:
  litellm_client.create_chat_completion(
    messages, tools=tool_schemas, model, temperature
  )
```

### Step 11 — Forward to LiteLLM proxy

```
LiteLLMClient → LiteLLM Proxy:
  POST  http://litellm_base_url/chat/completions
```

### Step 12 — Upstream LLM call

```
LiteLLM Proxy:  proxy → upstream LLM  (Gemini / Mistral / …)
```

### Step 13 — Receive completion response

```
LiteLLM Proxy → LiteLLMClient:
  ChatCompletion{
    choices[0].finish_reason,
    choices[0].message.tool_calls[]
  }
```

### Step 14 — Extract tool call

```
LiteLLMClient → AgentOrchestrator:
  _first_choice(response)  →  tool_call{id, name, arguments}
```

---

### [if `finish_reason == "tool_calls"`] — Execute tool call (timeout = 30 s)

### Step 15 — Dispatch to KnowCodeClient

```
AgentOrchestrator → KnowCodeClient:
  _execute_tool_call(tool_call)  →  knowcode_client.execute_tool(name, args)
```

### Step 16 — KnowCodeClient dispatches to REST API

KnowCodeClient maps tool names to REST endpoints:

| Tool name | HTTP call |
|---|---|
| `query_context` | `POST /api/v1/context/query  {query, limit, task_type}` |
| `search` | `GET  /api/v1/search?q=...` |
| `get_context` | `GET  /api/v1/context?target=...&task_type=...` |
| `trace_calls` | `GET  /api/v1/trace_calls/{entity_id}?direction=...&depth=...` |

### Step 17 — API result returned

```
KnowCode REST API → KnowCodeClient:  result JSON
```

### Step 18 — Record execution

```
KnowCodeClient → AgentOrchestrator:
  ToolExecutionRecord{
    tool_name,
    tool_call_id,
    arguments,
    success,
    latency_ms
  }
```

### Step 19 — Append result and continue loop

```
AgentOrchestrator:
  append tool_result to messages[]
  → continue loop
```

---

**Loop exits when:** `finish_reason == "stop"` OR `max_tool_rounds` reached.

---

## Final Response

### Step 19 — Build ChatResponse

```
AgentOrchestrator → Gateway:
  ChatResponse{
    answer,
    model,
    usage{},
    response_cost,
    finish_reason,
    selected_tools[],
    tool_executions[]
  }
```

### Step 20 — Return to caller

```
Gateway → User / IDE:  ChatResponse
```

---

## Smoke E2E — `scripts/smoke_e2e.py`

Used in CI post-deploy validation or run manually.

### Step 21 — Health check

```
smoke_e2e.py → Gateway:  GET /health
Gateway:  assert {status: "ok"}
```

### Step 22 — Tools check

```
smoke_e2e.py → Gateway:  GET /api/v1/tools
Gateway:  assert count ≥ 1 tool available
```

### Step 23 — Chat round-trip

```
smoke_e2e.py → Gateway:
  POST /api/v1/chat
  {message: "Use query_context and get_context to find search logic..."}
```

### Step 24 — Validate response

```
smoke_e2e.py:
  assert answer != ''
  assert len(tool_executions) ≥ SmokeConfig.min_tool_calls
  [optional: filter by specific tool_name]
```
