"""Exact declaration spans for a Vue script block, rebased onto ``.vue`` lines.

The Vue parser discovers *which* bindings a component exposes with Vue-aware
rules (``<script setup>`` top-level bindings, Options API ``data``/``methods``/
``computed``, ``defineProps``/``defineEmits``). This module supplies *where*
each of those bindings is declared by parsing the same script content with the
real JavaScript or TypeScript grammar and translating node coordinates into the
enclosing ``.vue`` file's one-based lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TYPESCRIPT_LANGS = frozenset({"ts", "tsx", "typescript"})

_DECLARATION_NODES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }
)
_VARIABLE_NODES = frozenset({"lexical_declaration", "variable_declaration"})
_MEMBER_NODES = frozenset({"method_definition", "pair", "public_field_definition"})
_PATTERN_NAME_NODES = frozenset(
    {
        "identifier",
        "shorthand_property_identifier_pattern",
        "property_identifier",
    }
)


@dataclass(frozen=True)
class DeclarationSpan:
    """One-based ``.vue`` line range covering a declaration."""

    line_start: int
    line_end: int


def _text(node: Any) -> str:
    return str(node.text.decode("utf8"))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        return value[1:-1]
    return value


class VueScriptIndex:
    """Maps names declared in a script block to their exact ``.vue`` spans."""

    def __init__(self, content: str, parser: Any, line_offset: int) -> None:
        """Build the index.

        Args:
            content: Raw text of the script block.
            parser: The ``TreeSitterParser`` whose grammar matches the block's
                ``lang``. Callers own the parser so one instance is reused
                across files.
            line_offset: One-based ``.vue`` line holding the block's first
                content character. Node line 0 maps to exactly this line.
        """
        self._line_offset = line_offset
        self._declarations: dict[str, DeclarationSpan] = {}
        self._members: dict[str, DeclarationSpan] = {}
        self._literals: dict[str, DeclarationSpan] = {}
        self._calls: dict[str, DeclarationSpan] = {}

        try:
            tree = parser.parser.parse(bytes(content, "utf8"))
        except Exception:  # pragma: no cover - grammar failures are non-fatal
            return
        self._collect(tree.root_node)

    # -- lookup ------------------------------------------------------------

    def lookup(self, name: str, *, prefer_member: bool = False) -> DeclarationSpan | None:
        """Return the best span for ``name``.

        Options API members and Composition API declarations can share a name,
        so callers state which category they are resolving.
        """
        order = (
            (self._members, self._declarations)
            if prefer_member
            else (self._declarations, self._members)
        )
        for table in order:
            span = table.get(name)
            if span is not None:
                return span
        return None

    def literal(self, name: str) -> DeclarationSpan | None:
        """Return the span of a string literal whose value is ``name``."""
        return self._literals.get(name)

    def call(self, callee: str) -> DeclarationSpan | None:
        """Return the span of a call to ``callee`` such as ``defineProps``."""
        return self._calls.get(callee)

    def fallback(self) -> DeclarationSpan:
        """Return the block's first content line for unresolvable names."""
        return DeclarationSpan(self._line_offset, self._line_offset)

    # -- collection --------------------------------------------------------

    def _span(self, node: Any) -> DeclarationSpan:
        return DeclarationSpan(
            self._line_offset + node.start_point[0],
            self._line_offset + node.end_point[0],
        )

    def _record(self, table: dict[str, DeclarationSpan], name: str, node: Any) -> None:
        if name:
            table.setdefault(name, self._span(node))

    def _collect(self, node: Any) -> None:
        node_type = node.type

        if node_type in _VARIABLE_NODES:
            for name in self._declarator_names(node):
                self._record(self._declarations, name, node)
        elif node_type in _DECLARATION_NODES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self._record(self._declarations, _text(name_node), node)
        elif node_type in _MEMBER_NODES:
            key = node.child_by_field_name("name") or node.child_by_field_name("key")
            if key is not None:
                self._record(self._members, _unquote(_text(key)), node)
        elif node_type == "string":
            self._record(self._literals, _unquote(_text(node)), node)
        elif node_type == "call_expression":
            function_node = node.child_by_field_name("function")
            if function_node is not None and function_node.type == "identifier":
                self._record(self._calls, _text(function_node), node)

        for child in node.children:
            self._collect(child)

    def _declarator_names(self, node: Any) -> list[str]:
        names: list[str] = []
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                names.extend(self._pattern_names(name_node))
        return names

    def _pattern_names(self, node: Any) -> list[str]:
        if node.type in _PATTERN_NAME_NODES:
            return [_text(node)]

        names: list[str] = []
        for child in node.children:
            names.extend(self._pattern_names(child))
        return names
