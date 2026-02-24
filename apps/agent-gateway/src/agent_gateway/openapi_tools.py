"""OpenAPI fetching and translation into OpenAI-compatible tool schemas."""

from __future__ import annotations

import copy
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_UNSUPPORTED_SCHEMA_KEYS = {
    "title",
    "example",
    "examples",
    "deprecated",
    "discriminator",
    "externalDocs",
    "xml",
    "readOnly",
    "writeOnly",
}
_HTTP_METHODS: Tuple[str, ...] = ("get", "post", "put", "patch", "delete")
_CANONICAL_TOOL_NAMES: Dict[Tuple[str, str], str] = {
    ("post", "/api/v1/context/query"): "query_context",
    ("get", "/api/v1/search"): "search",
    ("get", "/api/v1/context"): "get_context",
    ("get", "/api/v1/trace_calls/{entity_id}"): "trace_calls",
}


class OpenAPIFetchError(RuntimeError):
    """Raised when the OpenAPI spec cannot be fetched or parsed."""


class OpenAPITranslationError(RuntimeError):
    """Raised when the OpenAPI spec cannot be translated to tools."""


@dataclass
class OpenAPIToolRegistry:
    """Fetch and cache KnowCode OpenAPI spec, then map selected operations to tools."""

    knowcode_api_base_url: str
    timeout_seconds: float
    cache_ttl_seconds: int

    def __post_init__(self) -> None:
        self._translator = OpenAPIToolTranslator()
        self._cached_spec: Optional[Dict[str, Any]] = None
        self._cached_at: float = 0.0

    def get_tools(self, allowed_operation_ids: Sequence[str]) -> List[Dict[str, Any]]:
        spec = self._get_openapi_spec()
        return self._translator.translate(spec, allowed_operation_ids)

    def _get_openapi_spec(self) -> Dict[str, Any]:
        now = time.monotonic()
        if (
            self._cached_spec is not None
            and self.cache_ttl_seconds > 0
            and now - self._cached_at <= float(self.cache_ttl_seconds)
        ):
            return self._cached_spec

        spec = fetch_openapi_spec(
            self.knowcode_api_base_url,
            timeout_seconds=self.timeout_seconds,
        )
        self._cached_spec = spec
        self._cached_at = now
        return spec


class OpenAPIToolTranslator:
    """Translate OpenAPI 3.x operations into OpenAI tool definitions."""

    def translate(
        self,
        openapi_spec: Dict[str, Any],
        allowed_operation_ids: Sequence[str],
    ) -> List[Dict[str, Any]]:
        paths = openapi_spec.get("paths")
        if not isinstance(paths, dict):
            raise OpenAPITranslationError("OpenAPI spec is missing a valid 'paths' object")

        allowed_set = {operation_id for operation_id in allowed_operation_ids}
        tools: List[Dict[str, Any]] = []
        emitted_names: set[str] = set()

        for path, path_item, method, operation in self._iter_operations(openapi_spec):
            operation_id_raw = operation.get("operationId")
            operation_id = (
                str(operation_id_raw)
                if isinstance(operation_id_raw, str) and operation_id_raw.strip()
                else f"{method}_{path}"
            )
            operation_id = self._sanitize_tool_name(operation_id)
            canonical_name = self._canonical_tool_name(path=path, method=method)
            if canonical_name not in allowed_set and operation_id not in allowed_set:
                continue
            tool_name = canonical_name if canonical_name in allowed_set else operation_id
            if tool_name in emitted_names:
                continue

            parameters = self._build_parameters_schema(
                operation=operation,
                path_item=path_item,
                openapi_spec=openapi_spec,
            )
            description = self._compress_description(
                str(operation.get("summary") or operation.get("description") or "").strip(),
                fallback=f"{method.upper()} {path}",
            )

            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
            emitted_names.add(tool_name)

        return tools

    def _iter_operations(
        self,
        openapi_spec: Dict[str, Any],
    ) -> Iterable[Tuple[str, Dict[str, Any], str, Dict[str, Any]]]:
        paths = openapi_spec.get("paths", {})
        if not isinstance(paths, dict):
            return

        for path, path_item_raw in paths.items():
            if not isinstance(path_item_raw, dict):
                continue

            path_item = path_item_raw
            for method in _HTTP_METHODS:
                operation_raw = path_item.get(method)
                if not isinstance(operation_raw, dict):
                    continue
                yield str(path), path_item, method, operation_raw

    def _build_parameters_schema(
        self,
        operation: Dict[str, Any],
        path_item: Dict[str, Any],
        openapi_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

        merged_parameters: Dict[str, Dict[str, Any]] = {}
        for source in (
            path_item.get("parameters", []),
            operation.get("parameters", []),
        ):
            if not isinstance(source, list):
                continue
            for raw_parameter in source:
                parameter = self._resolve_refs(raw_parameter, openapi_spec, seen=set())
                if not isinstance(parameter, dict):
                    continue

                name = parameter.get("name")
                location = parameter.get("in", "query")
                if not isinstance(name, str) or not name:
                    continue

                key = f"{location}:{name}"
                merged_parameters[key] = parameter

        required_set = set()
        properties = schema["properties"]

        for parameter in merged_parameters.values():
            name = str(parameter.get("name"))
            parameter_schema = parameter.get("schema", {"type": "string"})
            normalized = self._normalize_schema(
                self._resolve_refs(parameter_schema, openapi_spec, seen=set())
            )
            properties[name] = normalized

            if bool(parameter.get("required", False)):
                required_set.add(name)

        request_body = self._resolve_refs(
            operation.get("requestBody", {}),
            openapi_spec,
            seen=set(),
        )
        if isinstance(request_body, dict):
            body_schema = self._extract_request_body_schema(request_body, openapi_spec)
            if body_schema is not None:
                normalized_body_schema = self._normalize_schema(body_schema)
                if normalized_body_schema.get("type") == "object":
                    body_properties = normalized_body_schema.get("properties", {})
                    if isinstance(body_properties, dict):
                        for prop_name, prop_schema in body_properties.items():
                            if not isinstance(prop_name, str):
                                continue
                            if prop_name not in properties:
                                properties[prop_name] = prop_schema

                    body_required = normalized_body_schema.get("required", [])
                    if isinstance(body_required, list):
                        for required_name in body_required:
                            if isinstance(required_name, str):
                                required_set.add(required_name)
                else:
                    properties["body"] = normalized_body_schema
                    if bool(request_body.get("required", False)):
                        required_set.add("body")

        if required_set:
            schema["required"] = sorted(required_set)
        else:
            schema.pop("required", None)

        return schema

    def _extract_request_body_schema(
        self,
        request_body: Dict[str, Any],
        openapi_spec: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        content = request_body.get("content", {})
        if not isinstance(content, dict):
            return None

        preferred = content.get("application/json")
        if isinstance(preferred, dict):
            schema = preferred.get("schema")
            if schema is not None:
                resolved = self._resolve_refs(schema, openapi_spec, seen=set())
                if isinstance(resolved, dict):
                    return resolved

        for media_type in content.values():
            if not isinstance(media_type, dict):
                continue
            schema = media_type.get("schema")
            if schema is None:
                continue
            resolved = self._resolve_refs(schema, openapi_spec, seen=set())
            if isinstance(resolved, dict):
                return resolved

        return None

    def _resolve_refs(self, node: Any, openapi_spec: Dict[str, Any], seen: set[str]) -> Any:
        if isinstance(node, list):
            return [self._resolve_refs(item, openapi_spec, seen=seen.copy()) for item in node]

        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = node.get("$ref")
            if not isinstance(ref, str):
                return {}
            if ref in seen:
                return {}

            resolved_target = self._resolve_ref_pointer(ref, openapi_spec)
            merged = copy.deepcopy(resolved_target)
            for key, value in node.items():
                if key == "$ref":
                    continue
                merged[key] = value

            next_seen = set(seen)
            next_seen.add(ref)
            return self._resolve_refs(merged, openapi_spec, seen=next_seen)

        resolved: Dict[str, Any] = {}
        for key, value in node.items():
            resolved[key] = self._resolve_refs(value, openapi_spec, seen=seen.copy())
        return resolved

    def _resolve_ref_pointer(self, ref: str, openapi_spec: Dict[str, Any]) -> Dict[str, Any]:
        if not ref.startswith("#/"):
            raise OpenAPITranslationError(f"Unsupported external ref: {ref}")

        node: Any = openapi_spec
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise OpenAPITranslationError(f"Invalid ref pointer: {ref}")
            node = node[part]

        if not isinstance(node, dict):
            raise OpenAPITranslationError(f"Ref does not resolve to an object: {ref}")
        return node

    def _normalize_schema(self, node: Any) -> Any:
        if isinstance(node, list):
            return [self._normalize_schema(item) for item in node]

        if not isinstance(node, dict):
            return node

        normalized: Dict[str, Any] = {}
        nullable = bool(node.get("nullable", False))

        for key, value in node.items():
            if key == "nullable":
                continue
            if key in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            normalized[key] = self._normalize_schema(value)

        if nullable:
            current_type = normalized.get("type")
            if isinstance(current_type, str):
                normalized["type"] = [current_type, "null"]
            elif isinstance(current_type, list):
                if "null" not in current_type:
                    normalized["type"] = [*current_type, "null"]

        return normalized

    def _compress_description(self, description: str, fallback: str) -> str:
        if not description:
            return fallback

        text = re.sub(r"\s+", " ", description).strip()
        if len(text) <= 180:
            return text
        return f"{text[:177].rstrip()}..."

    def _sanitize_tool_name(self, operation_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_]", "_", operation_id)
        sanitized = re.sub(r"_+", "_", sanitized)
        sanitized = sanitized.strip("_")
        return sanitized or "tool"

    def _canonical_tool_name(self, *, path: str, method: str) -> str:
        key = (method.lower(), path)
        return _CANONICAL_TOOL_NAMES.get(key, self._sanitize_tool_name(f"{method}_{path}"))



def fetch_openapi_spec(base_url: str, timeout_seconds: float) -> Dict[str, Any]:
    """Fetch and parse OpenAPI JSON from the KnowCode API server."""

    openapi_url = f"{base_url.rstrip('/')}/openapi.json"
    request = urllib.request.Request(openapi_url, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise OpenAPIFetchError(
            f"Failed to fetch OpenAPI schema from {openapi_url}"
        ) from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OpenAPIFetchError(f"Invalid JSON from {openapi_url}") from exc

    if not isinstance(parsed, dict):
        raise OpenAPIFetchError("OpenAPI document must be a JSON object")

    return parsed
