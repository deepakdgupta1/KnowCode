"""KnowCode HTTP client used for tool execution."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


class ToolExecutionError(RuntimeError):
    """Raised when a tool call against KnowCode fails."""


@dataclass
class KnowCodeClient:
    """Simple REST client for KnowCode endpoints used by the gateway."""

    base_url: str
    timeout_seconds: float

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if tool_name == "query_context":
            query = self._require_string(arguments, "query")
            body = {
                "query": query,
                "task_type": str(arguments.get("task_type", "general")),
                "limit": self._coerce_int(arguments.get("limit"), default=5),
                "expand_deps": self._coerce_bool(arguments.get("expand_deps"), default=True),
            }
            return self._request_json("POST", "/api/v1/context/query", body=body)

        if tool_name == "search":
            query = arguments.get("q") if "q" in arguments else arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ToolExecutionError("search requires argument 'q' or 'query'")
            params = {"q": query.strip()}
            return self._request_json("GET", "/api/v1/search", params=params)

        if tool_name == "get_context":
            target = self._require_string(arguments, "target")
            params = {
                "target": target,
                "max_tokens": self._coerce_int(arguments.get("max_tokens"), default=2000),
                "task_type": str(arguments.get("task_type", "general")),
            }
            return self._request_json("GET", "/api/v1/context", params=params)

        if tool_name == "trace_calls":
            entity_id = arguments.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id.strip():
                target = arguments.get("target")
                if isinstance(target, str) and target.strip():
                    entity_id = target

            if not isinstance(entity_id, str) or not entity_id.strip():
                raise ToolExecutionError("trace_calls requires argument 'entity_id' or 'target'")

            encoded_entity = urllib.parse.quote(entity_id, safe="")
            params = {
                "direction": str(arguments.get("direction", "callees")),
                "depth": self._coerce_int(arguments.get("depth"), default=1),
                "max_results": self._coerce_int(arguments.get("max_results"), default=50),
            }
            return self._request_json(
                "GET",
                f"/api/v1/trace_calls/{encoded_entity}",
                params=params,
            )

        raise ToolExecutionError(f"Unsupported tool requested: {tool_name}")

    def _request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        query_suffix = ""
        if params:
            query_suffix = f"?{urllib.parse.urlencode(params)}"

        url = f"{self.base_url.rstrip('/')}{path}{query_suffix}"
        payload: Optional[bytes] = None
        headers = {"Accept": "application/json"}

        if body is not None:
            payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=payload, method=method, headers=headers)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ToolExecutionError(f"KnowCode API error {exc.code} for {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"KnowCode API unreachable for {path}: {exc}") from exc

        if not raw_body.strip():
            return {}

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"KnowCode API returned invalid JSON for {path}") from exc

    def _require_string(self, values: Dict[str, Any], key: str) -> str:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolExecutionError(f"Missing required string argument: {key}")
        return value.strip()

    def _coerce_int(self, value: Any, default: int) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default
