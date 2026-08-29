"""Helpers for rename-resilient entity identity metadata."""

from __future__ import annotations

import hashlib
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from knowcode.data_models import Entity, EntityKind


class EndpointKind(str, Enum):
    """Classification for graph relationship endpoints."""

    INTERNAL = "internal"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"


_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")

_RESERVED_ENDPOINT_PREFIXES = {
    "composable",
    "external",
    "ref",
    "trait",
    "type",
    "unresolved",
    "vue_component",
}


def _require_component(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _encode_component(value: str, label: str, *, safe: str = "._-/@") -> str:
    return quote(_require_component(value, label), safe=safe)


def normalize_file_identity(file_path: str | Path) -> str:
    """Return the canonical absolute POSIX identity for a source path.

    ``resolve(strict=False)`` deliberately collapses symlink aliases while also
    supporting delete/watch events whose target no longer exists.
    """
    return Path(file_path).expanduser().resolve(strict=False).as_posix()


def build_internal_entity_id(
    file_path: str | Path,
    qualified_name: str,
) -> str:
    """Build a canonical ID for an entity extracted from repository source."""
    name = _require_component(qualified_name, "qualified_name")
    return f"{normalize_file_identity(file_path)}::{name}"


def build_external_reference_id(namespace: str, symbol: str) -> str:
    """Build an ID for a symbol known to be outside the indexed repository."""
    encoded_namespace = _encode_component(namespace, "namespace", safe="._-")
    encoded_symbol = _encode_component(symbol, "symbol")
    return f"external::{encoded_namespace}::{encoded_symbol}"


def build_unresolved_reference_id(
    language: str,
    file_path: str | Path,
    scope: str,
    symbol: str,
) -> str:
    """Build a scoped ID for a reference that cannot be resolved locally."""
    encoded_language = _encode_component(language, "language", safe="._-")
    encoded_file = quote(normalize_file_identity(file_path), safe="/._-")
    encoded_scope = _encode_component(scope, "scope", safe="._-")
    encoded_symbol = _encode_component(symbol, "symbol")
    return (
        f"unresolved::{encoded_language}::{encoded_file}::"
        f"{encoded_scope}::{encoded_symbol}"
    )


def classify_endpoint_id(endpoint_id: str) -> EndpointKind:
    """Classify a canonical graph endpoint without guessing legacy prefixes."""
    parts = endpoint_id.split("::")
    if parts[0] == "external":
        return (
            EndpointKind.EXTERNAL
            if len(parts) == 3 and all(parts[1:])
            else EndpointKind.INVALID
        )
    if parts[0] == "unresolved":
        return (
            EndpointKind.UNRESOLVED
            if len(parts) == 5 and all(parts[1:])
            else EndpointKind.INVALID
        )
    if parts[0] in _RESERVED_ENDPOINT_PREFIXES:
        return EndpointKind.INVALID

    try:
        file_identity, qualified_name = endpoint_id.rsplit("::", 1)
    except ValueError:
        return EndpointKind.INVALID

    if not qualified_name or not Path(file_identity).is_absolute():
        return EndpointKind.INVALID
    if normalize_file_identity(file_identity) != file_identity:
        return EndpointKind.INVALID
    return EndpointKind.INTERNAL


def canonicalize_source_snippet(source: str) -> str:
    """Normalize source text so semantically identical snippets hash consistently."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]

    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def compute_entity_content_hash(entity: Entity) -> str:
    """Compute a stable SHA-256 hash for an entity's canonical snippet."""
    canonical_source = canonicalize_source_snippet(entity.source_code or "")
    if canonical_source:
        payload = canonical_source
    else:
        # Fallback for entities without source snippets (e.g., derived/system entities).
        payload = "\n".join(
            part
            for part in (
                entity.kind.value,
                entity.signature or "",
                entity.docstring or "",
                entity.name,
            )
            if part
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pack_content_hash(value: object) -> object:
    """Return a hex digest as its raw bytes, other values unchanged.

    Entities are hashed with SHA-256 and chunks with MD5, so this packs any
    even-length lowercase hex string rather than one fixed width. Those are
    exactly the inputs :func:`unpack_content_hash` renders back character for
    character. Anything else is stored as it arrived.
    """
    if isinstance(value, str) and value and len(value) % 2 == 0:
        if all(char in _LOWERCASE_HEX_DIGITS for char in value):
            return bytes.fromhex(value)
    return value


def unpack_content_hash(value: object) -> object:
    """Render a stored digest back as the lowercase hex callers expect."""
    return value.hex() if isinstance(value, bytes) else value


def ensure_entity_content_hash(entity: Entity) -> None:
    """Populate metadata.content_hash when missing."""
    existing = entity.metadata.get("content_hash")
    if isinstance(existing, str) and existing:
        return
    entity.metadata["content_hash"] = compute_entity_content_hash(entity)


def dedupe_entities_by_id(
    entities: list[Entity],
) -> tuple[list[Entity], list[str]]:
    """Drop entities that share a canonical ID, keeping the first occurrence.

    A parser must never emit two entities with the same canonical ID. When two
    declarations collide (for example ``class A {}`` twice in one file), the
    second is a silent drop that :class:`GraphBuilder` would otherwise collapse
    by dict assignment. This helper makes that drop visible instead: it keeps
    the first entity per ID and returns one diagnostic message per collision so
    the limitation is classified through ``ParseResult.errors`` rather than lost.

    If a concrete declaration (class, function, etc.) shares an ID with a
    preceding synthetic file-level MODULE entity, the concrete declaration
    supersedes the module entity without collision reporting.
    """
    seen: dict[str, int] = {}
    deduped: list[Entity] = []
    errors: list[str] = []
    for entity in entities:
        if entity.id in seen:
            prev_idx = seen[entity.id]
            prev_entity = deduped[prev_idx]
            if (
                prev_entity.kind == EntityKind.MODULE
                and entity.kind != EntityKind.MODULE
            ):
                deduped[prev_idx] = entity
                continue
            errors.append(
                f"Duplicate entity identity {entity.qualified_name!r}; "
                "keeping the first declaration"
            )
            continue
        seen[entity.id] = len(deduped)
        deduped.append(entity)
    return deduped, errors
