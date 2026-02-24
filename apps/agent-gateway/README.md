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
- `GET /ready`
- `GET /api/v1/tools`
- `POST /api/v1/chat`

## Local Self-Hosted Mode (KnowCode + Gateway on your machine)

You can run everything locally with one command:

```bash
# Required: export keys in your shell profile or current terminal
export GOOGLE_API_KEY_1="..."
export LITELLM_MASTER_KEY="sk-your-local-master-key"
export LITELLM_API_KEY="$LITELLM_MASTER_KEY"

# Optional override (default works for local Docker + local KnowCode)
export KNOWCODE_API_BASE_URL="http://host.docker.internal:8000"

apps/agent-gateway/scripts/local_up.sh
```

Stop all services:

```bash
apps/agent-gateway/scripts/local_down.sh
```

Notes:
- `local_up.sh` starts KnowCode API on `0.0.0.0:8000` if it is not already running.
- It then starts LiteLLM + gateway via Docker Compose and waits for `/ready`.
- Ollama fallback remains configured. If Ollama is installed and running on your host (`:11434`), LiteLLM can route to it.

## End-to-End Smoke Test

With KnowCode API, LiteLLM, and this gateway running, execute:

```bash
uv run --project apps/agent-gateway python scripts/smoke_e2e.py
```

Optional strict tool set:

```bash
uv run --project apps/agent-gateway python scripts/smoke_e2e.py \
  --tool-names query_context,get_context \
  --min-tool-calls 1 \
  --print-json
```

## Docker Run

```bash
cd apps/agent-gateway
docker compose up --build
```

This starts LiteLLM + the gateway. KnowCode is expected at
`http://host.docker.internal:8000` by default.

## Production Notes

- Use `AGENT_STRICT_ENV_VALIDATION=true` for non-local environments.
- Keep secrets in a secret manager, not checked-in files.
- Use `.env.production.example` as a template only.
- Read `OPERATIONS.md` for rollout, readiness probes, monitoring, and rollback.

## CI Workflows

- `.github/workflows/agent-gateway-ci.yml`
  - Runs Ruff + pytest for `apps/agent-gateway`
  - Validates Docker build
- `.github/workflows/agent-gateway-smoke.yml`
  - Manual post-deploy smoke test against a live gateway URL

## Extraction Checklist (To New Repo)

1. Copy `apps/agent-gateway` into a new repository root.
2. Keep env variables and names unchanged.
3. Move CI workflow to run tests in `tests/`.
4. Deploy as an independent service (no shared filesystem needed).
5. Set `KNOWCODE_API_BASE_URL` to deployed KnowCode API URL.

No code changes should be required if you keep the env contract.
