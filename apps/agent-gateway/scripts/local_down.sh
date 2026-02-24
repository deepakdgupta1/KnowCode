#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
GATEWAY_DIR="${PROJECT_ROOT}/apps/agent-gateway"

echo "Stopping Docker Compose services"
cd "${GATEWAY_DIR}"
docker compose down || true

if [[ -f /tmp/knowcode-api.pid ]]; then
  PID="$(cat /tmp/knowcode-api.pid || true)"
  if [[ -n "${PID}" ]] && kill -0 "${PID}" >/dev/null 2>&1; then
    echo "Stopping KnowCode API process ${PID}"
    kill "${PID}" || true
  fi
  rm -f /tmp/knowcode-api.pid
fi

echo "Local stack stopped."
