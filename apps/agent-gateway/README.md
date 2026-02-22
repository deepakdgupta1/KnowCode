# Agent Gateway (Extractable App)

This app is a standalone orchestration layer that sits between your client and KnowCode.

It is intentionally isolated so you can move it into a separate GitHub repository later
without changing application logic.

## Responsibilities

- Fetch KnowCode OpenAPI schema (`/openapi.json`)
- Translate selected endpoints into OpenAI/LiteLLM tool schemas
- Call LiteLLM (`/v1/chat/completions`) with tagged metadata
- Execute tool calls against KnowCode REST endpoints
- Return final answer, usage data, response cost, and tool execution trace

## Explicit Boundaries

- `apps/agent-gateway` imports **nothing** from `src/knowcode`
- Integration happens only over HTTP:
  - KnowCode API (`KNOWCODE_API_BASE_URL`)
  - LiteLLM proxy (`LITELLM_BASE_URL`)
- Runtime config comes only from environment variables

These boundaries are what make repo extraction clean.

## Local Run

```bash
# 1) Start KnowCode API in another terminal (from repo root)
uvicorn knowcode.api.main:create_app --factory --port 8000

# 2) Install gateway deps
uv sync --project apps/agent-gateway --extra dev

# 3) Run gateway
uv run --project apps/agent-gateway agent-gateway
```

Gateway endpoints:
- `GET /health`
- `GET /api/v1/tools`
- `POST /api/v1/chat`

## Docker Run

```bash
cd apps/agent-gateway
docker compose up --build
```

This starts LiteLLM + the gateway. KnowCode is expected at
`http://host.docker.internal:8000` by default.

## Extraction Checklist (To New Repo)

1. Copy `apps/agent-gateway` into a new repository root.
2. Keep env variables and names unchanged.
3. Move CI workflow to run tests in `tests/`.
4. Deploy as an independent service (no shared filesystem needed).
5. Set `KNOWCODE_API_BASE_URL` to deployed KnowCode API URL.

No code changes should be required if you keep the env contract.
