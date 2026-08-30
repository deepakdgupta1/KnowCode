"""One place that answers what language an entity is written in.

Two callers with two different needs. A language *census* wants every entity
to land in some bucket, so an unrecognised suffix becomes ``"unknown"``. A
markdown *fence* wants no tag at all in that case: ```` ```unknown ```` is a
literal, wrong claim about the code, where a bare fence simply says nothing.
"""

from __future__ import annotations

from pathlib import Path

from knowcode.data_models import Entity

#: Suffixes whose language name differs from the suffix itself, or which map
#: several suffixes onto one language. Anything absent falls back to the
#: suffix, which is already right for `.go`, `.rb`, `.php` and most others.
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".md": "markdown",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".rs": "rust",
    ".vue": "vue",
}

UNKNOWN_LANGUAGE = "unknown"


def language_for(entity: Entity) -> str:
    """The entity's language: what the parser recorded, else its suffix.

    Returns ``UNKNOWN_LANGUAGE`` when neither is available, so every entity
    lands in a bucket. Use :func:`fence_tag_for` when the answer is going into
    a code fence.
    """
    language = entity.metadata.get("language")
    if isinstance(language, str) and language:
        return language

    if not entity.location or not entity.location.file_path:
        return UNKNOWN_LANGUAGE

    suffix = Path(entity.location.file_path).suffix.lower()
    return LANGUAGE_BY_SUFFIX.get(suffix, suffix.lstrip(".") or UNKNOWN_LANGUAGE)


def fence_tag_for(entity: Entity) -> str:
    """The info string for a fenced code block, or ``""`` when unknown."""
    language = language_for(entity)
    return "" if language == UNKNOWN_LANGUAGE else language
