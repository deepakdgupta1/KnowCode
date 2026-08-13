"""Scope-aware identity and reference resolution for Rust sources.

Rust references are resolved against the declarations of one lexical module
scope. A name that is not declared exactly once in that scope stays an explicit
unresolved reference; it is never resolved by scanning the whole file for a
first match, and it never becomes a fabricated ``type::``/``trait::`` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from knowcode.utils.entity_identity import (
    build_internal_entity_id,
    build_unresolved_reference_id,
)

LANGUAGE = "rust"

#: Tree-sitter node types that declare a type-like name, and their ``rust_type``.
TYPE_DECLARATION_NODES = {
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "type_item": "type_alias",
}

#: ``rust_type`` values an ``impl <Type>`` header may name.
IMPLEMENTABLE_TYPES = frozenset({"struct", "enum", "type_alias"})

#: ``rust_type`` values an ``impl <Trait> for`` header may name.
TRAIT_TYPES = frozenset({"trait"})


def strip_generics(type_text: str) -> str:
    """Return a type path without its generic arguments.

    ``HashMap<K, V>`` becomes ``HashMap`` and ``vendor::Render`` is unchanged.
    """
    return type_text.split("<", 1)[0].strip()


def qualified_segment(type_text: str) -> str:
    """Return the qualified-name segment for a Rust type path.

    Rust path separators become qualified-name separators so an entity ID never
    embeds a second ``::`` delimiter.
    """
    return strip_generics(type_text).replace("::", ".")


@dataclass(frozen=True)
class RustDeclaration:
    """A type-like declaration that later references may resolve against."""

    name: str
    qualified_name: str
    rust_type: str


@dataclass(frozen=True)
class RustImplOwner:
    """The type an impl block's methods belong to."""

    #: Base type path with generic arguments removed, e.g. ``Point``.
    type_name: str
    #: Qualified-name segment for the type, e.g. ``Point`` or ``crate.Point``.
    segment: str
    #: Real entity ID when the type is declared in this scope, otherwise ``None``.
    entity_id: Optional[str]


@dataclass(frozen=True)
class RustScope:
    """One lexical Rust module scope and the names it can resolve."""

    parent_id: str
    qualified_name: str
    types: Mapping[str, RustDeclaration] = field(default_factory=dict)
    functions: Mapping[str, str] = field(default_factory=dict)
    methods: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def qualify(self, name: str) -> str:
        """Return the qualified name of a declaration made in this scope."""
        return f"{self.qualified_name}.{name}"


@dataclass
class RustFileState:
    """Per-file identity bookkeeping and classified parser limitations."""

    file_path: Path
    declared: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def claim(self, qualified_name: str, declaration_kind: str) -> bool:
        """Reserve a qualified name, reporting and rejecting duplicates.

        Two declarations cannot share one canonical ID, so the later one is
        dropped and reported instead of silently overwriting the first.
        """
        if qualified_name in self.declared:
            self.errors.append(
                f"Duplicate Rust {declaration_kind} identity '{qualified_name}'; "
                "keeping the first declaration"
            )
            return False
        self.declared.add(qualified_name)
        return True


def resolve_type_endpoint(
    scope: RustScope,
    file_path: Path,
    type_text: str,
    source_scope: str,
    expected: frozenset[str],
) -> Optional[str]:
    """Resolve a type or trait path to a real entity or an unresolved reference.

    Returns ``None`` when the path carries no usable name at all.
    """
    base = strip_generics(type_text)
    if not base:
        return None

    declaration = scope.types.get(base) if base.isidentifier() else None
    if declaration is not None and declaration.rust_type in expected:
        return build_internal_entity_id(file_path, declaration.qualified_name)

    return build_unresolved_reference_id(LANGUAGE, file_path, source_scope, base)


def resolve_callee_endpoint(
    scope: RustScope,
    file_path: Path,
    callee_text: str,
    source_scope: str,
) -> Optional[str]:
    """Resolve a Rust callee to a local entity or an unresolved reference.

    Only unambiguous local callees resolve: a bare function declared once in
    this scope, or ``Type::method`` naming a method extracted from this scope.
    """
    callee = callee_text.strip()
    if not callee:
        return None

    if callee.isidentifier():
        local_function = scope.functions.get(callee)
        if local_function is not None:
            return build_internal_entity_id(file_path, local_function)
    else:
        segments = callee.split("::")
        if len(segments) == 2 and all(part.isidentifier() for part in segments):
            type_methods = scope.methods.get(segments[0], {})
            method_qualified_name = type_methods.get(segments[1])
            if method_qualified_name is not None:
                return build_internal_entity_id(file_path, method_qualified_name)

    return build_unresolved_reference_id(LANGUAGE, file_path, source_scope, callee)
