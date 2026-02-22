"""Rust parser using Tree-sitter."""

from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.base import TreeSitterParser


class RustParser(TreeSitterParser):
    """Parses Rust source files."""

    def __init__(self) -> None:
        """Initialize Rust parser."""
        super().__init__("rust")

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract entities from Rust AST."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Traverse all children nodes
        for child in node.children:
            child_type = child.type

            if child_type == "macro_invocation":
                # Capture macro calls (println!, vec!, custom macros)
                macro_rel = self._parse_macro_invocation(child, parent_id)
                if macro_rel:
                    relationships.append(macro_rel)

            elif child_type == "struct_item":
                struct_entities, struct_rels = self._parse_struct(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(struct_entities)
                relationships.extend(struct_rels)

            elif child_type == "enum_item":
                enum_entity, enum_rels = self._parse_enum(
                    child, file_path, parent_id, source_code, source_lines
                )
                if enum_entity:
                    entities.append(enum_entity)
                relationships.extend(enum_rels)

            elif child_type == "trait_item":
                trait_entity, trait_rels = self._parse_trait(
                    child, file_path, parent_id, source_code, source_lines
                )
                if trait_entity:
                    entities.append(trait_entity)
                relationships.extend(trait_rels)

            elif child_type == "impl_item":
                impl_entities, impl_rels = self._parse_impl(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(impl_entities)
                relationships.extend(impl_rels)

            elif child_type == "function_item":
                func_entity, func_rels = self._parse_function(
                    child, file_path, parent_id, source_code, source_lines
                )
                if func_entity:
                    entities.append(func_entity)
                relationships.extend(func_rels)

            elif child_type == "const_item":
                const_entity = self._parse_const(
                    child, file_path, parent_id, source_code, source_lines
                )
                if const_entity:
                    entities.append(const_entity)

            elif child_type == "static_item":
                static_entity = self._parse_static(
                    child, file_path, parent_id, source_code, source_lines
                )
                if static_entity:
                    entities.append(static_entity)

            elif child_type == "type_item":
                type_entity = self._parse_type_alias(
                    child, file_path, parent_id, source_code, source_lines
                )
                if type_entity:
                    entities.append(type_entity)

            elif child_type == "mod_item":
                mod_entities, mod_rels = self._parse_module(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(mod_entities)
                relationships.extend(mod_rels)

            elif child_type == "use_declaration":
                use_rels = self._parse_use_declaration(child, parent_id)
                relationships.extend(use_rels)

        return entities, relationships

    def _parse_struct(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse a struct declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return [], []

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"

        # Extract doc comment, visibility, and attributes
        docstring = self._extract_doc_comment(node, source_lines)
        visibility = self._extract_visibility(node)
        attributes = self._extract_attributes(node, source_lines)

        struct_entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Rust structs map to CLASS
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=docstring,
        )

        # Add metadata
        struct_entity.metadata["visibility"] = visibility
        struct_entity.metadata["rust_type"] = "struct"
        if attributes:
            struct_entity.metadata["attributes"] = "|".join(attributes)

        entities = [struct_entity]
        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=struct_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        # Extract fields
        body_node = node.child_by_field_name("body")
        if body_node:
            for field in body_node.children:
                if field.type == "field_declaration":
                    field_name_node = field.child_by_field_name("name")
                    field_type_node = field.child_by_field_name("type")

                    if field_name_node:
                        field_name = self._get_text(field_name_node)
                        field_qualified = f"{qualified_name}.{field_name}"

                        field_entity = self._create_entity(
                            field,
                            EntityKind.VARIABLE,
                            field_name,
                            field_qualified,
                            file_path,
                            source_lines,
                        )
                        entities.append(field_entity)
                        relationships.append(
                            Relationship(
                                source_id=struct_entity.id,
                                target_id=field_entity.id,
                                kind=RelationshipKind.CONTAINS,
                            )
                        )

                        # Track type usage (strip generics for base type)
                        if field_type_node:
                            type_name = self._get_text(field_type_node)
                            base_type = self._strip_generics(type_name)
                            relationships.append(
                                Relationship(
                                    source_id=field_entity.id,
                                    target_id=f"type::{base_type}",
                                    kind=RelationshipKind.USES_TYPE,
                                )
                            )

        return entities, relationships

    def _parse_enum(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[Optional[Entity], list[Relationship]]:
        """Parse an enum declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None, []

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        docstring = self._extract_doc_comment(node, source_lines)
        visibility = self._extract_visibility(node)
        attributes = self._extract_attributes(node, source_lines)

        enum_entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Enums map to CLASS
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=docstring,
        )

        # Add metadata
        enum_entity.metadata["visibility"] = visibility
        enum_entity.metadata["rust_type"] = "enum"
        if attributes:
            enum_entity.metadata["attributes"] = "|".join(attributes)

        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=enum_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        return enum_entity, relationships

    def _parse_trait(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[Optional[Entity], list[Relationship]]:
        """Parse a trait declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None, []

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        docstring = self._extract_doc_comment(node, source_lines)
        visibility = self._extract_visibility(node)
        attributes = self._extract_attributes(node, source_lines)

        trait_entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Traits map to INTERFACE
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=docstring,
        )

        # Add metadata
        trait_entity.metadata["visibility"] = visibility
        trait_entity.metadata["rust_type"] = "trait"
        if attributes:
            trait_entity.metadata["attributes"] = "|".join(attributes)

        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=trait_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        return trait_entity, relationships

    def _parse_impl(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse an impl block.

        Handles both:
        - Inherent impls: impl MyStruct { ... }
        - Trait impls: impl MyTrait for MyStruct { ... }
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Get the type being implemented
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")

        # CRITICAL: Ensure type_name is ALWAYS captured, never "unknown"
        if not type_node:
            # Fallback: This should never happen in valid Rust code
            return entities, relationships

        type_name = self._get_text(type_node)
        if not type_name:
            return entities, relationships

        # Determine if this is inherent or trait impl
        is_trait_impl = trait_node is not None

        # If implementing a trait, create relationship
        # Strip generics from trait name for cleaner linking
        trait_name = None
        if is_trait_impl:
            trait_name_raw = self._get_text(trait_node)
            trait_name = self._strip_generics(trait_name_raw)
            relationships.append(
                Relationship(
                    source_id=f"type::{type_name}",
                    target_id=f"trait::{trait_name}",
                    kind=RelationshipKind.IMPLEMENTS,
                )
            )

        # Extract methods from impl block
        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                if child.type == "function_item":
                    method_entity, method_rels = self._parse_function(
                        child, file_path, parent_id, source_code, source_lines, impl_type=type_name
                    )
                    if method_entity:
                        entities.append(method_entity)
                        relationships.extend(method_rels)

                        # Associate method with type
                        # This ensures methods are NEVER orphaned
                        relationships.append(
                            Relationship(
                                source_id=f"type::{type_name}",
                                target_id=method_entity.id,
                                kind=RelationshipKind.CONTAINS,
                            )
                        )

                        # Add metadata to method
                        method_entity.metadata["impl_type"] = "trait" if is_trait_impl else "inherent"
                        method_entity.metadata["associated_type"] = type_name

                        # If this is a trait impl, link the method to the trait as well
                        if is_trait_impl and trait_name:
                            method_entity.metadata["implemented_trait"] = trait_name
                            # Create a direct relationship for "Where is Clone implemented?"
                            relationships.append(
                                Relationship(
                                    source_id=method_entity.id,
                                    target_id=f"trait::{trait_name}",
                                    kind=RelationshipKind.IMPLEMENTS,
                                )
                            )

        return entities, relationships

    def _parse_function(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        impl_type: Optional[str] = None,
    ) -> tuple[Optional[Entity], list[Relationship]]:
        """Parse a function declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None, []

        name = self._get_text(name_node)

        # Build qualified name
        if impl_type:
            qualified_name = f"{parent_id.split('::')[-1]}.{impl_type}.{name}"
        else:
            qualified_name = f"{parent_id.split('::')[-1]}.{name}"

        # Extract signature (resolve Self if in impl block)
        signature = self._extract_function_signature(node, source_code, impl_type)
        docstring = self._extract_doc_comment(node, source_lines)
        visibility = self._extract_visibility(node)
        attributes = self._extract_attributes(node, source_lines)

        func_entity = self._create_entity(
            node,
            EntityKind.FUNCTION,
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=docstring,
            signature=signature,
        )

        # Add metadata
        func_entity.metadata["visibility"] = visibility
        if impl_type:
            func_entity.metadata["is_method"] = "true"
        else:
            func_entity.metadata["is_method"] = "false"

        # Capture attributes (especially #[test] for test detection)
        if attributes:
            func_entity.metadata["attributes"] = "|".join(attributes)
            # Flag test functions
            if any("#[test]" in attr or "#[tokio::test]" in attr or "#[async_test]" in attr for attr in attributes):
                func_entity.metadata["is_test"] = "true"

        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=func_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        # Extract function calls from body
        body_node = node.child_by_field_name("body")
        if body_node:
            call_rels = self._extract_calls_from_body(body_node, func_entity.id)
            relationships.extend(call_rels)

        return func_entity, relationships

    def _parse_const(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> Optional[Entity]:
        """Parse a const declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        visibility = self._extract_visibility(node)

        entity = self._create_entity(
            node,
            EntityKind.VARIABLE,
            name,
            qualified_name,
            file_path,
            source_lines,
        )

        # Add metadata
        entity.metadata["visibility"] = visibility
        entity.metadata["is_const"] = "true"

        return entity

    def _parse_static(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> Optional[Entity]:
        """Parse a static declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        visibility = self._extract_visibility(node)

        entity = self._create_entity(
            node,
            EntityKind.VARIABLE,
            name,
            qualified_name,
            file_path,
            source_lines,
        )

        # Add metadata
        entity.metadata["visibility"] = visibility
        entity.metadata["is_static"] = "true"

        return entity

    def _parse_type_alias(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> Optional[Entity]:
        """Parse a type alias declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        visibility = self._extract_visibility(node)

        entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Type aliases map to CLASS
            name,
            qualified_name,
            file_path,
            source_lines,
        )

        # Add metadata
        entity.metadata["visibility"] = visibility
        entity.metadata["rust_type"] = "type_alias"

        return entity

    def _parse_module(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse a module declaration.

        Handles both:
        - Inline modules: mod utils { ... }
        - External modules: mod utils; (points to utils.rs or utils/mod.rs)
        """
        name_node = node.child_by_field_name("name")
        if not name_node:
            return [], []

        name = self._get_text(name_node)
        qualified_name = f"{parent_id.split('::')[-1]}.{name}"
        visibility = self._extract_visibility(node)

        # Check if this is an external module (mod foo;) or inline module (mod foo { ... })
        body_node = node.child_by_field_name("body")
        is_external = body_node is None

        mod_entity = self._create_entity(
            node,
            EntityKind.MODULE,
            name,
            qualified_name,
            file_path,
            source_lines,
        )

        # Add metadata
        mod_entity.metadata["visibility"] = visibility
        mod_entity.metadata["is_external"] = "true" if is_external else "false"

        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=mod_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        if is_external:
            # Signal that this module is defined in an external file
            # The scanner should look for:
            # 1. <current_dir>/<module_name>.rs
            # 2. <current_dir>/<module_name>/mod.rs
            current_dir = file_path.parent
            possible_paths = [
                current_dir / f"{name}.rs",
                current_dir / name / "mod.rs"
            ]
            mod_entity.metadata["possible_file_paths"] = "|".join([str(p) for p in possible_paths])
        else:
            # Inline module - recursively parse its contents
            if body_node:
                nested_entities, nested_rels = self._extract_entities(
                    body_node,
                    file_path,
                    mod_entity.id,  # Module becomes the parent
                    source_code,
                    source_lines,
                )
                # Return both the module and its nested entities
                return [mod_entity, *nested_entities], relationships + nested_rels

        return [mod_entity], relationships

    def _parse_use_declaration(self, node: Any, parent_id: str) -> list[Relationship]:
        """Parse use declarations (imports).

        Handles simple and complex imports:
        - use std::collections::HashMap;
        - use std::{vec, collections::HashMap};
        """
        relationships = []

        # Get the use tree (what's being imported)
        use_tree = node.child_by_field_name("argument")
        if use_tree:
            # Parse use tree recursively to handle complex imports
            imported_paths = self._parse_use_tree(use_tree)

            for path in imported_paths:
                relationships.append(
                    Relationship(
                        source_id=parent_id,
                        target_id=f"external::{path}",
                        kind=RelationshipKind.IMPORTS,
                    )
                )

        return relationships

    def _extract_function_signature(self, node: Any, source_code: str, impl_type: Optional[str] = None) -> str:
        """Extract function signature from node, resolving Self if needed."""
        # Get function parameters
        params_node = node.child_by_field_name("parameters")
        return_type_node = node.child_by_field_name("return_type")

        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node) if name_node else "unknown"

        params_text = self._get_text(params_node) if params_node else "()"
        return_text = self._get_text(return_type_node) if return_type_node else ""

        # Resolve Self keyword in return type
        if return_text and impl_type:
            return_text = self._resolve_self_keyword(return_text, impl_type)

        if return_text:
            return f"fn {name}{params_text} {return_text}"
        return f"fn {name}{params_text}"

    def _extract_calls_from_body(self, body_node: Any, caller_id: str) -> list[Relationship]:
        """Extract function calls from a function body."""
        relationships: list[Relationship] = []

        def visit_node(node: Any) -> None:
            if node.type == "call_expression":
                # Get function being called
                function_node = node.child_by_field_name("function")
                if function_node:
                    callee_name = self._get_text(function_node)
                    relationships.append(
                        Relationship(
                            source_id=caller_id,
                            target_id=f"function::{callee_name}",
                            kind=RelationshipKind.CALLS,
                        )
                    )

            # Recursively visit children
            for child in node.children:
                visit_node(child)

        visit_node(body_node)
        return relationships

    def _extract_doc_comment(self, node: Any, source_lines: list[str]) -> Optional[str]:
        """Extract documentation comment (///) before a node.

        Handles attributes like #[derive(Debug)] between doc and struct.
        """
        start_line = node.start_point[0]

        # Look backwards for doc comments
        doc_lines: list[str] = []
        line_idx = start_line - 1

        while line_idx >= 0:
            line = source_lines[line_idx].strip()
            if line.startswith("///"):
                # Remove /// prefix and add to doc
                doc_lines.insert(0, line[3:].strip())
                line_idx -= 1
            elif line.startswith("//!"):
                # Module-level doc comment
                doc_lines.insert(0, line[3:].strip())
                line_idx -= 1
            elif line == "" or line.startswith("//") or line.startswith("#["):
                # Skip empty lines, regular comments, and attributes
                line_idx -= 1
            else:
                break

        if doc_lines:
            return "\n".join(doc_lines)
        return None

    def _parse_macro_invocation(self, node: Any, parent_id: str) -> Optional[Relationship]:
        """Parse a macro invocation (e.g., println!, vec!)."""
        # Get macro name
        macro_node = node.child_by_field_name("macro")
        if not macro_node:
            return None

        macro_name = self._get_text(macro_node)

        # Create CALLS relationship for the macro
        return Relationship(
            source_id=parent_id,
            target_id=f"macro::{macro_name}",
            kind=RelationshipKind.CALLS,
        )

    def _resolve_self_keyword(self, type_text: str, impl_type: Optional[str]) -> str:
        """Resolve 'Self' keyword to actual type name in impl blocks."""
        if impl_type and "Self" in type_text:
            return type_text.replace("Self", impl_type)
        return type_text

    def _parse_use_tree(self, use_tree_node: Any) -> list[str]:
        """Parse a use tree recursively to extract all imported paths.

        Handles complex imports like:
        - use std::collections::HashMap;
        - use std::{vec, collections::HashMap};
        - use std::collections::{HashMap, BTreeMap};
        """
        paths: list[str] = []

        if use_tree_node.type == "use_list":
            # Multiple imports: use std::{vec, collections}
            for child in use_tree_node.children:
                if child.type != ",":  # Skip comma separators
                    sub_paths = self._parse_use_tree(child)
                    paths.extend(sub_paths)

        elif use_tree_node.type == "scoped_use_list":
            # Scoped list: use std::collections::{HashMap, BTreeMap}
            path_node = use_tree_node.child_by_field_name("path")
            list_node = use_tree_node.child_by_field_name("list")

            if path_node and list_node:
                prefix = self._get_text(path_node)
                sub_paths = self._parse_use_tree(list_node)
                paths.extend([f"{prefix}::{sub}" for sub in sub_paths])

        else:
            # Simple import: use std::vec or an identifier
            path_text = self._get_text(use_tree_node)
            if path_text:
                paths.append(path_text.strip())

        return paths

    def _extract_visibility(self, node: Any) -> str:
        """Extract visibility modifier from a node.

        Returns one of: "public", "pub(crate)", "pub(super)", "pub(in path)", "private"
        """
        # Look for visibility_modifier child
        for child in node.children:
            if child.type == "visibility_modifier":
                vis_text = self._get_text(child)
                if vis_text == "pub":
                    return "public"
                elif vis_text.startswith("pub("):
                    return vis_text  # pub(crate), pub(super), etc.

        return "private"  # Default in Rust

    def _extract_attributes(self, node: Any, source_lines: list[str]) -> list[str]:
        """Extract attributes (#[...]) before a node.

        Captures attributes like #[test], #[derive(Debug)], #[inline], etc.
        """
        start_line = node.start_point[0]
        attributes: list[str] = []
        line_idx = start_line - 1

        while line_idx >= 0:
            line = source_lines[line_idx].strip()
            if line.startswith("#["):
                # Capture the full attribute
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

    def _strip_generics(self, type_text: str) -> str:
        """Strip generic parameters from a type name.

        Examples:
        - HashMap<K, V> -> HashMap
        - Vec<String> -> Vec
        - Option<T> -> Option
        """
        if "<" in type_text:
            return type_text.split("<")[0].strip()
        return type_text
