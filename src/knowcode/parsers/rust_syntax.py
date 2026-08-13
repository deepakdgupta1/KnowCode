"""Rust surface-syntax helpers shared by the Rust parser.

These functions read node text, doc comments, attributes, visibility, and use
trees. They hold no parser state so they can be exercised directly.
"""

from __future__ import annotations

from typing import Any, Optional


def node_text(node: Any) -> str:
    """Return the decoded source text of a Tree-sitter node."""
    return str(node.text.decode("utf8"))


def extract_doc_comment(node: Any, source_lines: list[str]) -> Optional[str]:
    """Extract the ``///`` documentation comment preceding a node.

    Attributes such as ``#[derive(Debug)]`` may sit between the doc comment and
    the declaration.
    """
    doc_lines: list[str] = []
    line_idx = node.start_point[0] - 1

    while line_idx >= 0:
        line = source_lines[line_idx].strip()
        if line.startswith("///") or line.startswith("//!"):
            doc_lines.insert(0, line[3:].strip())
            line_idx -= 1
        elif line == "" or line.startswith("//") or line.startswith("#["):
            # Skip empty lines, regular comments, and attributes
            line_idx -= 1
        else:
            break

    return "\n".join(doc_lines) if doc_lines else None


def extract_attributes(node: Any, source_lines: list[str]) -> list[str]:
    """Extract the ``#[...]`` attributes preceding a node, outermost first."""
    attributes: list[str] = []
    line_idx = node.start_point[0] - 1

    while line_idx >= 0:
        line = source_lines[line_idx].strip()
        if line.startswith("#["):
            attributes.insert(0, line)
            line_idx -= 1
        elif line.startswith("///") or line.startswith("//!"):
            # Skip doc comments
            line_idx -= 1
        elif line == "" or line.startswith("//"):
            # Skip empty lines and regular comments
            line_idx -= 1
        else:
            break

    return attributes


def extract_visibility(node: Any) -> str:
    """Return a node's visibility: ``public``, ``pub(...)``, or ``private``."""
    for child in node.children:
        if child.type == "visibility_modifier":
            visibility = node_text(child)
            if visibility == "pub":
                return "public"
            if visibility.startswith("pub("):
                return visibility  # pub(crate), pub(super), pub(in path)

    return "private"  # Default in Rust


def resolve_self_keyword(type_text: str, self_type: Optional[str]) -> str:
    """Replace ``Self`` with the implementing type name inside an impl block."""
    if self_type and "Self" in type_text:
        return type_text.replace("Self", self_type)
    return type_text


def extract_function_signature(node: Any, self_type: Optional[str] = None) -> str:
    """Build a function signature, resolving ``Self`` in the return type."""
    name_node = node.child_by_field_name("name")
    params_node = node.child_by_field_name("parameters")
    return_type_node = node.child_by_field_name("return_type")

    name = node_text(name_node) if name_node is not None else "unknown"
    params_text = node_text(params_node) if params_node is not None else "()"
    return_text = node_text(return_type_node) if return_type_node is not None else ""

    if return_text:
        return f"fn {name}{params_text} {resolve_self_keyword(return_text, self_type)}"
    return f"fn {name}{params_text}"


def parse_use_tree(node: Any) -> list[str]:
    """Extract every imported path from a use tree.

    Handles ``use std::collections::HashMap;``, ``use std::{vec, io};``,
    ``use std::fmt::{self, Display};``, and ``use std::io::Write as IoWrite;``.
    """
    paths: list[str] = []
    node_type = node.type

    if node_type == "use_list":
        # Only named children are paths; braces and commas are plain tokens.
        for child in node.named_children:
            paths.extend(parse_use_tree(child))

    elif node_type == "scoped_use_list":
        path_node = node.child_by_field_name("path")
        list_node = node.child_by_field_name("list")
        if path_node is not None and list_node is not None:
            prefix = node_text(path_node).strip()
            # `self` in a use list imports the prefix module itself.
            paths.extend(
                prefix if sub == "self" else f"{prefix}::{sub}"
                for sub in parse_use_tree(list_node)
            )

    elif node_type == "use_as_clause":
        # Aliased import: the imported target is the path, not the local alias.
        path_node = node.child_by_field_name("path")
        if path_node is not None:
            paths.extend(parse_use_tree(path_node))

    else:
        # Simple import: use std::vec, or a bare identifier
        path_text = node_text(node).strip()
        if path_text:
            paths.append(path_text)

    return paths
