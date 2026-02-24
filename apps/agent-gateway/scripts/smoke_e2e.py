#!/usr/bin/env python3
"""End-to-end smoke test for the KnowCode Agent Gateway."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class SmokeConfig:
    """Runtime options for smoke validation."""

    gateway_url: str
    message: str
    timeout_seconds: float
    min_tool_calls: int
    tool_names: Optional[List[str]]
    print_json: bool


class SmokeError(RuntimeError):
    """Raised when smoke checks fail."""



def _normalize_base_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise SmokeError("Gateway URL cannot be empty")
    return value.rstrip("/")



def _http_json(
    method: str,
    url: str,
    *,
    timeout_seconds: float,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Optional[bytes] = None
    headers = {"Accept": "application/json"}

    if body is not None:
        payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=payload, method=method, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmokeError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"Request failed for {url}: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"Invalid JSON response from {url}") from exc

    if not isinstance(parsed, dict):
        raise SmokeError(f"Expected JSON object from {url}")

    return parsed



def _check_health(config: SmokeConfig) -> None:
    health_url = f"{config.gateway_url}/health"
    health = _http_json("GET", health_url, timeout_seconds=config.timeout_seconds)
    if health.get("status") != "ok":
        raise SmokeError("Gateway health check failed")



def _check_tools(config: SmokeConfig) -> Dict[str, Any]:
    tools_url = f"{config.gateway_url}/api/v1/tools"
    if config.tool_names:
        encoded = urllib.parse.urlencode([("names", name) for name in config.tool_names])
        tools_url = f"{tools_url}?{encoded}"

    tools_payload = _http_json("GET", tools_url, timeout_seconds=config.timeout_seconds)
    count = tools_payload.get("count")
    if not isinstance(count, int) or count < 1:
        raise SmokeError("No tools returned by gateway")
    return tools_payload



def _run_chat(config: SmokeConfig) -> Dict[str, Any]:
    chat_url = f"{config.gateway_url}/api/v1/chat"
    payload: Dict[str, Any] = {
        "message": config.message,
        "temperature": 0.0,
    }
    if config.tool_names:
        payload["tool_names"] = config.tool_names

    response = _http_json("POST", chat_url, timeout_seconds=config.timeout_seconds, body=payload)

    answer = response.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise SmokeError("Gateway response is missing a usable answer")

    executions = response.get("tool_executions")
    if not isinstance(executions, list):
        raise SmokeError("Gateway response missing tool_executions list")

    if len(executions) < config.min_tool_calls:
        raise SmokeError(
            f"Expected at least {config.min_tool_calls} tool call(s), got {len(executions)}"
        )

    successful = 0
    for item in executions:
        if not isinstance(item, dict):
            continue
        if bool(item.get("success")):
            successful += 1

    if successful < config.min_tool_calls:
        raise SmokeError(
            "Tool execution did not complete successfully for required minimum calls"
        )

    return response



def _parse_tool_names(raw: str) -> Optional[List[str]]:
    values = [part.strip() for part in raw.split(",")]
    selected = [part for part in values if part]
    return selected or None



def parse_args(argv: Sequence[str]) -> SmokeConfig:
    parser = argparse.ArgumentParser(description="Run E2E smoke test for agent gateway")
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8081",
        help="Gateway base URL (default: http://127.0.0.1:8081)",
    )
    parser.add_argument(
        "--message",
        default=(
            "Use query_context and get_context to identify where search logic lives in "
            "KnowCode, then answer in one concise sentence."
        ),
        help="Prompt to send to /api/v1/chat",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=45.0,
        help="HTTP timeout for each request",
    )
    parser.add_argument(
        "--min-tool-calls",
        type=int,
        default=1,
        help="Minimum successful tool executions required",
    )
    parser.add_argument(
        "--tool-names",
        default="",
        help="Optional comma-separated tool names to force in the chat request",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print full chat JSON response",
    )

    args = parser.parse_args(argv)

    if args.min_tool_calls < 1:
        raise SmokeError("--min-tool-calls must be >= 1")
    if args.timeout_seconds <= 0:
        raise SmokeError("--timeout-seconds must be > 0")

    return SmokeConfig(
        gateway_url=_normalize_base_url(args.gateway_url),
        message=args.message,
        timeout_seconds=float(args.timeout_seconds),
        min_tool_calls=int(args.min_tool_calls),
        tool_names=_parse_tool_names(args.tool_names),
        print_json=bool(args.print_json),
    )



def run(config: SmokeConfig) -> int:
    _check_health(config)
    tools_payload = _check_tools(config)
    chat_payload = _run_chat(config)

    tool_executions = chat_payload.get("tool_executions", [])
    selected_tools = chat_payload.get("selected_tools", [])
    answer = str(chat_payload.get("answer", "")).strip()
    model = str(chat_payload.get("model", ""))
    response_cost = chat_payload.get("response_cost")
    usage = chat_payload.get("usage") if isinstance(chat_payload.get("usage"), dict) else {}

    print("Smoke test passed")
    print(f"Gateway: {config.gateway_url}")
    print(f"Tools advertised: {tools_payload.get('count')}")
    print(f"Tools selected: {selected_tools}")
    print(f"Tool executions: {len(tool_executions)}")
    print(f"Model: {model}")
    print(f"Response cost: {response_cost}")
    print(
        "Usage tokens: "
        f"prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}, "
        f"total={usage.get('total_tokens')}"
    )
    print(f"Answer preview: {answer[:180]}")

    if config.print_json:
        print(json.dumps(chat_payload, indent=2, ensure_ascii=True))

    return 0



def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_args(list(argv) if argv is not None else sys.argv[1:])
        return run(config)
    except SmokeError as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
