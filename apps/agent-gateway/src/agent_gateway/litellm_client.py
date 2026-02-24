"""LiteLLM proxy client using OpenAI-compatible chat completions."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class LiteLLMError(RuntimeError):
    """Raised when communication with LiteLLM fails."""


@dataclass
class LiteLLMResult:
    """Parsed LiteLLM response and normalized response cost."""

    payload: Dict[str, Any]
    response_cost: Optional[float]


@dataclass
class LiteLLMClient:
    """HTTP client for LiteLLM `/v1/chat/completions`."""

    base_url: str
    api_key: str
    timeout_seconds: float

    def check_health(self) -> Dict[str, Any]:
        """Check whether LiteLLM is reachable and serving requests."""
        try:
            return self._get_json("/health")
        except LiteLLMError as health_error:
            # Some LiteLLM deployments may not expose /health, so fall back to /v1/models.
            try:
                return self._get_json("/v1/models")
            except LiteLLMError:
                raise health_error

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tags: List[str],
        temperature: float,
    ) -> LiteLLMResult:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "metadata": {"tags": tags},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        raw_payload = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        url = f"{self.base_url.rstrip('/')}/v1/chat/completions"

        request = urllib.request.Request(
            url,
            data=raw_payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                response_cost = self._extract_response_cost(response.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LiteLLMError(f"LiteLLM error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LiteLLMError(f"LiteLLM unreachable: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LiteLLMError("LiteLLM returned invalid JSON") from exc

        if not isinstance(parsed, dict):
            raise LiteLLMError("LiteLLM response must be a JSON object")

        response_cost = response_cost or self._extract_response_cost(parsed)
        return LiteLLMResult(payload=parsed, response_cost=response_cost)

    def _get_json(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LiteLLMError(f"LiteLLM health error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LiteLLMError(f"LiteLLM unreachable: {exc}") from exc

        if not body.strip():
            return {"status": "ok"}

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {"status": "ok"}

        if isinstance(parsed, dict):
            return parsed
        return {"status": "ok"}

    def _extract_response_cost(self, source: Any) -> Optional[float]:
        if source is None:
            return None

        values: List[Any] = []
        if isinstance(source, dict):
            values.extend(
                [
                    source.get("response_cost"),
                    source.get("x-litellm-response-cost"),
                    source.get("x-litellm-response_cost"),
                ]
            )
        else:
            for header_name in (
                "x-litellm-response-cost",
                "x-litellm-response_cost",
                "response_cost",
            ):
                value = getattr(source, "get", lambda _key: None)(header_name)
                values.append(value)

        for value in values:
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None
