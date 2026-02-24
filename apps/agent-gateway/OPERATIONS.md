# Agent Gateway Operations Runbook

## 1) Preflight

1. Confirm KnowCode API and LiteLLM URLs are reachable from gateway runtime.
2. Provision secrets in your secret manager:
   - `LITELLM_API_KEY`
   - upstream model provider keys used by LiteLLM (for example `GOOGLE_API_KEY_1`)
3. Use `AGENT_STRICT_ENV_VALIDATION=true` in non-local environments.
4. Restrict tool scope with `AGENT_ALLOWED_TOOL_NAMES`.

### Local self-host mode

1. Export env vars on your machine:
   - `GOOGLE_API_KEY_1`
   - `LITELLM_MASTER_KEY`
   - `LITELLM_API_KEY` (typically same as `LITELLM_MASTER_KEY`)
2. Run `apps/agent-gateway/scripts/local_up.sh`.
3. Run smoke test:
   - `uv run --project apps/agent-gateway python apps/agent-gateway/scripts/smoke_e2e.py --gateway-url http://127.0.0.1:8081`

## 2) CI Gate

CI workflow: `.github/workflows/agent-gateway-ci.yml`

1. Lint: `ruff check apps/agent-gateway/src apps/agent-gateway/tests apps/agent-gateway/scripts`
2. Tests: `pytest apps/agent-gateway/tests`
3. Docker build validation (`apps/agent-gateway/Dockerfile`)

## 3) Deploy

1. Deploy gateway as its own service/container.
2. Configure readiness probe:
   - `GET /ready` (checks KnowCode API, LiteLLM, and OpenAPI tool translation)
3. Configure liveness probe:
   - `GET /health`
4. Roll out with rolling deployment by default.

## 4) Post-Deploy Smoke

Use `.github/workflows/agent-gateway-smoke.yml` or run manually:

```bash
uv run --project apps/agent-gateway python apps/agent-gateway/scripts/smoke_e2e.py \
  --gateway-url https://your-gateway.example.com \
  --min-tool-calls 1
```

## 5) Monitoring and Alerts

The gateway emits JSON logs for:

1. `http_request` with request path, status code, and latency.
2. `chat_completed` with model, tool counts, response cost, and token usage.
3. `chat_failed` with failure type and error detail.

Set alerts on:

1. `GET /ready` failures.
2. Elevated `chat_failed` rate.
3. Tool timeout spikes (`failed_tool_calls` or high `total_tool_latency_ms`).
4. Cost anomalies (`response_cost` trends).

## 6) Rollback

1. Keep prior image digest available.
2. If readiness or error-rate SLO is breached, roll back to previous stable image.
3. Re-run smoke against rolled-back version.
