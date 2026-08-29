"""Lexically-scoped, collision-free identity for document headings.

ADR 1 requires an entity's qualified name to carry its lexical scope, the way
``module.Type.method`` does for code. A document's lexical scope is its heading
hierarchy, so a section under ``# Guide`` is ``<document>.guide.<slug>``.

Deriving the name from the slug alone, as both prose parsers once did, made two
distinct entities share an ID whenever a document's H1 slugified to its own
filename, or whenever two headings shared a title. The chunker then emitted
duplicate chunk IDs and the file was rejected from the index outright (BL-1).
Prefixing the parent scope removes the first collision by construction; a
document entity is ``<stem>`` and no section can ever be shorter than
``<stem>.<slug>``. The second needs an ordinal, because two sibling headings
may legitimately carry the same title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STRIP_PATTERN = re.compile(r"[^\w\s-]")
_SEPARATOR_PATTERN = re.compile(r"[\s_]+")
_DASH_RUN_PATTERN = re.compile(r"-+")

#: Slug given to a heading whose title is entirely punctuation or emoji. The
#: ordinal below then keeps several of them apart.
UNNAMED_SLUG = "section"


def slugify(text: str) -> str:
    """Reduce a heading title to a lowercase, dash-separated slug."""
    slug = _STRIP_PATTERN.sub("", text.lower().strip())
    slug = _SEPARATOR_PATTERN.sub("-", slug)
    return _DASH_RUN_PATTERN.sub("-", slug).strip("-")


@dataclass(frozen=True)
class Heading:
    """One heading resolved against the scope it was declared in."""

    entity_id: str
    qualified_name: str
    parent_id: str


@dataclass(frozen=True)
class _Frame:
    level: int
    entity_id: str
    qualified_name: str


class HeadingScope:
    """Resolves a document's headings, in order, into unique entity IDs.

    Drive it heading by heading in source order. It owns the open-scope stack
    that both prose parsers used to keep by hand, and the set of names already
    issued for this document.
    """

    def __init__(self, file_path: str, document_name: str) -> None:
        self._file_path = file_path
        self._document_id = f"{file_path}::{document_name}"
        self._stack = [
            _Frame(level=0, entity_id=self._document_id, qualified_name=document_name)
        ]
        self._issued = {document_name}

    @property
    def document_id(self) -> str:
        """ID of the document entity every top-level heading hangs from."""
        return self._document_id

    def enter(self, level: int, title: str) -> Heading:
        """Resolve one heading and open its scope for the headings that follow.

        Args:
            level: Heading depth, 1 for the outermost. Levels need not be
                contiguous; a jump from 1 to 3 nests under the level 1.
            title: The heading text as written.

        Returns:
            The heading's entity ID, its scoped qualified name, and the ID of
            the entity that ``contains`` it.
        """
        while len(self._stack) > 1 and self._stack[-1].level >= level:
            self._stack.pop()

        parent = self._stack[-1]
        qualified_name = self._issue(
            parent.qualified_name, slugify(title) or UNNAMED_SLUG
        )
        entity_id = f"{self._file_path}::{qualified_name}"

        self._stack.append(
            _Frame(level=level, entity_id=entity_id, qualified_name=qualified_name)
        )
        return Heading(
            entity_id=entity_id,
            qualified_name=qualified_name,
            parent_id=parent.entity_id,
        )

    def _issue(self, parent_name: str, slug: str) -> str:
        """Claim the first unused name under ``parent_name`` for ``slug``."""
        candidate = f"{parent_name}.{slug}"
        ordinal = 1
        while candidate in self._issued:
            ordinal += 1
            candidate = f"{parent_name}.{slug}-{ordinal}"
        self._issued.add(candidate)
        return candidate
