#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
GATEWAY_DIR="${PROJECT_ROOT}/apps/agent-gateway"
KNOWCODE_VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"

export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-local-proxy}"
export LITELLM_API_KEY="${LITELLM_API_KEY:-${LITELLM_MASTER_KEY}}"
export KNOWCODE_API_BASE_URL="${KNOWCODE_API_BASE_URL:-http://host.docker.internal:8000}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not installed." >&2
  exit 1
fi

if ! ss -ltn | rg -q ':8000'; then
  echo "Starting KnowCode API on 0.0.0.0:8000"
  # Use setsid so the API process survives after this script exits.
  if [[ -x "${KNOWCODE_VENV_PYTHON}" ]]; then
    setsid env PYTHONPATH="${PROJECT_ROOT}/src:${PYTHONPATH:-}" \
      "${KNOWCODE_VENV_PYTHON}" -m uvicorn knowcode.api.main:create_app \
      --factory --host 0.0.0.0 --port 8000 > /tmp/knowcode-api.log 2>&1 < /dev/null &
  else
    setsid uv run uvicorn knowcode.api.main:create_app --factory --host 0.0.0.0 --port 8000 \
      > /tmp/knowcode-api.log 2>&1 < /dev/null &
  fi
  echo "$!" > /tmp/knowcode-api.pid
else
  echo "KnowCode API already listening on :8000"
fi

echo "Waiting for KnowCode OpenAPI..."
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8000/openapi.json" >/dev/null; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:8000/openapi.json" >/dev/null; then
  echo "KnowCode API did not become ready. Check /tmp/knowcode-api.log" >&2
  exit 1
fi

echo "Starting LiteLLM + Agent Gateway with Docker Compose"
cd "${GATEWAY_DIR}"
if docker buildx version >/dev/null 2>&1; then
  docker compose up -d --build
else
  DOCKER_BUILDKIT=0 docker compose up -d --build
fi

echo "Waiting for gateway readiness..."
for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:8081/ready" >/dev/null; then
    break
  fi
  sleep 1
done

curl -fsS "http://127.0.0.1:8081/ready" >/dev/null
echo "Local stack is ready:"
echo "  KnowCode API: http://127.0.0.1:8000"
echo "  AI Gateway:   http://127.0.0.1:8081"
