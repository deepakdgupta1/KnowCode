"""JavaScript parser using Tree-sitter."""

from pathlib import Path
from typing import Any

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.base import TreeSitterParser


class JavaScriptParser(TreeSitterParser):
    """Parses JavaScript/TypeScript files."""

    def __init__(self) -> None:
        """Initialize JavaScript parser."""
        super().__init__("javascript")

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract entities from JavaScript AST."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Traverse children in the current scope.
        for child in node.children:
            child_type = child.type

            if child_type == "class_declaration":
                class_entities, class_rels = self._parse_class(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(class_entities)
                relationships.extend(class_rels)

            elif child_type == "function_declaration":
                func_entity, func_rels = self._parse_function(
                    child, file_path, parent_id, source_code, source_lines, kind=EntityKind.FUNCTION
                )
                entities.append(func_entity)
                relationships.extend(func_rels)

            elif child_type in {"variable_declaration", "lexical_declaration"}:
                var_entities, var_rels = self._parse_variable_declaration(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(var_entities)
                relationships.extend(var_rels)

            elif child_type == "export_statement":
                exported = child.child_by_field_name("declaration") or child.child_by_field_name("value")
                if not exported:
                    continue

                if exported.type == "class_declaration":
                    class_entities, class_rels = self._parse_class(
                        exported, file_path, parent_id, source_code, source_lines
                    )
                    entities.extend(class_entities)
                    relationships.extend(class_rels)
                elif exported.type == "function_declaration":
                    func_entity, func_rels = self._parse_function(
                        exported, file_path, parent_id, source_code, source_lines, kind=EntityKind.FUNCTION
                    )
                    entities.append(func_entity)
                    relationships.extend(func_rels)
                elif exported.type == "function":
                    func_entity, func_rels = self._parse_function(
                        exported,
                        file_path,
                        parent_id,
                        source_code,
                        source_lines,
                        kind=EntityKind.FUNCTION,
                        fallback_name="default_export",
                    )
                    entities.append(func_entity)
                    relationships.extend(func_rels)
                elif exported.type in {"variable_declaration", "lexical_declaration"}:
                    var_entities, var_rels = self._parse_variable_declaration(
                        exported, file_path, parent_id, source_code, source_lines
                    )
                    entities.extend(var_entities)
                    relationships.extend(var_rels)

            elif child_type == "import_statement":
                source_node = child.child_by_field_name("source")
                if source_node:
                    module_name = self._get_text(source_node).strip("'\"")
                    relationships.append(
                        Relationship(
                            source_id=parent_id,
                            target_id=f"external::{module_name}",
                            kind=RelationshipKind.IMPORTS
                        )
                    )

            elif child_type == "call_expression":
                call_rel = self._extract_call(child, parent_id)
                if call_rel:
                    relationships.append(call_rel)

        return entities, relationships

    def _parse_variable_declaration(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract function-like entities assigned to variables."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for decl in node.children:
            if decl.type != "variable_declarator":
                continue

            var_name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            fallback_name = self._get_text(var_name_node) if var_name_node else "anonymous"
            if not value_node:
                continue

            if value_node.type == "arrow_function":
                func_entity, func_rels = self._parse_arrow_function(
                    value_node,
                    var_name_node,
                    file_path,
                    parent_id,
                    source_code,
                    source_lines,
                    fallback_name=fallback_name,
                )
                entities.append(func_entity)
                relationships.extend(func_rels)
            elif value_node.type == "function":
                func_entity, func_rels = self._parse_function(
                    value_node,
                    file_path,
                    parent_id,
                    source_code,
                    source_lines,
                    kind=EntityKind.FUNCTION,
                    fallback_name=fallback_name,
                )
                entities.append(func_entity)
                relationships.extend(func_rels)

        return entities, relationships

    def _parse_class(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        name_node = node.child_by_field_name("name")
        if not name_node:
            return [], [] # Anonymous class

        class_name = self._get_text(name_node)
        qualified_name = class_name # Simplified for now
        class_id = f"{file_path}::{qualified_name}"
        
        # Check parent info for inheritance
        # In tree-sitter-javascript: class_heritage -> extends_clause -> call_expression or identifier
        # Actually structure is class_declaration -> class_heritage -> extends_clause
        # Let's check node children for "class_heritage"
        
        for child in node.children:
            if child.type == "class_heritage":
                 # extends Foo
                 extends_node = child.child_by_field_name("super_class") # or just iterate
                 if extends_node: # Usually the last child of heritage
                     base_name = self._get_text(extends_node)
                     relationships.append(
                        Relationship(
                            source_id=class_id,
                            target_id=f"ref::{base_name}",
                            kind=RelationshipKind.INHERITS
                        )
                     )

        entity = self._create_entity(
            node, EntityKind.CLASS, class_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        
        relationships.append(
            Relationship(source_id=parent_id, target_id=class_id, kind=RelationshipKind.CONTAINS)
        )

        body_node = node.child_by_field_name("body")
        if body_node:
            # Parse methods
            for child in body_node.children:
                if child.type == "method_definition":
                    method_entity, method_rels = self._parse_function(
                        child, file_path, class_id, source_code, source_lines, kind=EntityKind.METHOD, parent_name=class_name
                    )
                    entities.append(method_entity)
                    relationships.extend(method_rels)

        return entities, relationships

    def _parse_function(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        kind: EntityKind,
        parent_name: str = "",
        fallback_name: str | None = None,
    ) -> tuple[Entity, list[Relationship]]:
        name_node = node.child_by_field_name("name")
        if not name_node:
            if kind == EntityKind.METHOD:
                if self._get_text(node).startswith("constructor"):
                    name = "constructor"
                else:
                    name = fallback_name or "anonymous"
            else:
                name = fallback_name or "anonymous"
        else:
            name = self._get_text(name_node)

        if parent_name:
            qualified_name = f"{parent_name}.{name}"
        else:
            qualified_name = name

        func_id = f"{file_path}::{qualified_name}"

        entity = self._create_entity(
            node, kind, name, qualified_name, file_path, source_lines
        )

        relationships = [
            Relationship(source_id=parent_id, target_id=func_id, kind=RelationshipKind.CONTAINS)
        ]

        # Extract calls from body
        body_node = node.child_by_field_name("body")
        if body_node:
            calls = self._walk_for_calls(body_node, func_id)
            relationships.extend(calls)

        return entity, relationships

    def _parse_arrow_function(
        self,
        node: Any,
        name_node: Any | None,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        fallback_name: str | None = None,
    ) -> tuple[Entity, list[Relationship]]:
        name = self._get_text(name_node) if name_node else (fallback_name or "anonymous")
        func_id = f"{file_path}::{name}"

        entity = self._create_entity(
            node, EntityKind.FUNCTION, name, name, file_path, source_lines
        )

        relationships = [
            Relationship(source_id=parent_id, target_id=func_id, kind=RelationshipKind.CONTAINS)
        ]

        body_node = node.child_by_field_name("body")
        if body_node:
            calls = self._walk_for_calls(body_node, func_id)
            relationships.extend(calls)

        return entity, relationships

    def _walk_for_calls(self, node, source_id) -> Any:  # type: ignore
        """Recursive walk to find call_expression.
        
        Note: We use a cursor walk here which is significantly more performant 
        than recursively creating Node objects for every child in the tree.
        """
        rels = []
        cursor = node.walk()

        visited_children = False
        
        while True:
            if cursor.node.type == "call_expression":
                rel = self._extract_call(cursor.node, source_id)
                if rel:
                    rels.append(rel)
            
            # Traverse
            if not visited_children and cursor.goto_first_child():
                visited_children = False
                continue
            
            if cursor.goto_next_sibling():
                visited_children = False
                continue
            
            if cursor.goto_parent():
                visited_children = True
                if cursor.node == node: # Back at start
                    break
                continue
            else:
                break
                
        return rels

    def _extract_call(self, node, source_id) -> Any:  # type: ignore
        # call_expression: function: (identifier) arguments: (arguments)
        func_node = node.child_by_field_name("function")
        if not func_node:
            return None
        
        callee_name = self._get_text(func_node)
        # Verify it's not a keyword/syntax
        if " " in callee_name or "\n" in callee_name:
            # Complex expression call like (a+b)() or require('foo')
            # Handle require specially?
             if callee_name == "require":
                 # handle require('module')
                 args = node.child_by_field_name("arguments")
                 if args and args.named_child_count > 0:
                     first_arg = args.named_child(0)
                     if first_arg.type == "string":
                         module = self._get_text(first_arg).strip("'\"")
                         return Relationship(
                             source_id=source_id,
                             target_id=f"external::{module}",
                             kind=RelationshipKind.IMPORTS # Treat as import
                         )
             return None
             
        return Relationship(
            source_id=source_id,
            target_id=f"ref::{callee_name}",
            kind=RelationshipKind.CALLS
        )
