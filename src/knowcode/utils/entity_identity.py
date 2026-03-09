"""Helpers for rename-resilient entity identity metadata."""

from __future__ import annotations

import hashlib

from knowcode.data_models import Entity


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


def ensure_entity_content_hash(entity: Entity) -> None:
    """Populate metadata.content_hash when missing."""
    existing = entity.metadata.get("content_hash")
    if isinstance(existing, str) and existing:
        return
    entity.metadata["content_hash"] = compute_entity_content_hash(entity)
