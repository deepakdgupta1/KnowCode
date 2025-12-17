"""Java parser using Tree-sitter."""

from pathlib import Path
from typing import Any

from knowcode.models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.base import TreeSitterParser


class JavaParser(TreeSitterParser):
    """Parses Java files."""

    def __init__(self) -> None:
        """Initialize Java parser."""
        super().__init__("java")

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract entities from Java AST."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []


        
        # In Java, file usually maps to a class, but we have a module entity for the file anyway.
        # Package declaration defines the logic module/package.
        
        for child in node.children:
            child_type = child.type
            
            if child_type == "package_declaration":
                # package com.example;
                # This informs the "qualified name" of subsequent classes
                pass 
                
            elif child_type == "import_declaration":
                # import java.util.List;
                # No field name 'name', just find the identifier or scoped_identifier
                name_node = None
                for c in child.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        name_node = c
                        break
                
                if name_node:
                    imported_name = self._get_text(name_node, None)
                    relationships.append(
                        Relationship(
                            source_id=parent_id,
                            target_id=f"external::{imported_name}",
                            kind=RelationshipKind.IMPORTS
                        )
                    )
            
            elif child_type == "class_declaration":
                class_entities, class_rels = self._parse_class(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(class_entities)
                relationships.extend(class_rels)
                
            elif child_type == "interface_declaration":
                 # Treat interface as class for MVP
                class_entities, class_rels = self._parse_class(
                    child, file_path, parent_id, source_code, source_lines, kind=EntityKind.CLASS
                )
                entities.extend(class_entities)
                relationships.extend(class_rels)

        return entities, relationships

    def _parse_class(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        kind: EntityKind = EntityKind.CLASS
    ) -> tuple[list[Entity], list[Relationship]]:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        name_node = node.child_by_field_name("name")
        class_name = self._get_text(name_node, None)
        qualified_name = class_name # Simplified
        class_id = f"{file_path}::{qualified_name}"

        # Inheritance
        # superclass -> type_id
        # interfaces -> type_list
        superclass_node = node.child_by_field_name("superclass")
        if superclass_node:
             # extends Foo
             # superclass node contains type_identifier "Foo"
             # Actually, superclass: (superclass (type_identifier))
             # Just get text of the whole node for now
             base_text = self._get_text(superclass_node, None).replace("extends ", "").strip()
             relationships.append(
                Relationship(
                    source_id=class_id,
                    target_id=f"ref::{base_text}",
                    kind=RelationshipKind.INHERITS
                )
             )

        entity = self._create_entity(
            node, kind, class_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        
        relationships.append(
            Relationship(source_id=parent_id, target_id=class_id, kind=RelationshipKind.CONTAINS)
        )

        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                if child.type == "method_declaration":
                    method_entities, method_rels = self._parse_method(
                        child, file_path, class_id, source_code, source_lines, parent_name=class_name
                    )
                    entities.extend(method_entities)
                    relationships.extend(method_rels)
                
                elif child.type == "constructor_declaration":
                     # Handle constructor as method
                    method_entities, method_rels = self._parse_method(
                        child, file_path, class_id, source_code, source_lines, parent_name=class_name
                    )
                    entities.extend(method_entities)
                    relationships.extend(method_rels)

        return entities, relationships

    def _parse_method(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        parent_name: str
    ) -> tuple[list[Entity], list[Relationship]]:
        entities = []
        rels = []
        
        name_node = node.child_by_field_name("name")
        method_name = self._get_text(name_node, None)
        qualified_name = f"{parent_name}.{method_name}"
        method_id = f"{file_path}::{qualified_name}"
        
        entity = self._create_entity(
            node, EntityKind.METHOD, method_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        
        rels.append(
            Relationship(source_id=parent_id, target_id=method_id, kind=RelationshipKind.CONTAINS)
        )
        
        # Calls
        body_node = node.child_by_field_name("body")
        if body_node:
             calls = self._walk_for_calls(body_node, method_id)
             rels.extend(calls)
             
        return entities, rels

    def _walk_for_calls(self, node, source_id):
        rels = []
        cursor = node.walk()
        visited_children = False
        
        while True:
            if cursor.node.type == "method_invocation":
                # foo.bar(args)
                # object: (identifier), name: (identifier), arguments
                # OR bar(args) -> name: (identifier), arguments
                
                name_node = cursor.node.child_by_field_name("name")
                object_node = cursor.node.child_by_field_name("object")
                
                method_name = self._get_text(name_node, None)
                if object_node:
                    obj_name = self._get_text(object_node, None)
                    callee = f"{obj_name}.{method_name}"
                else:
                    callee = method_name
                
                rels.append(
                    Relationship(
                        source_id=source_id,
                        target_id=f"ref::{callee}",
                        kind=RelationshipKind.CALLS
                    )
                )

            elif cursor.node.type == "object_creation_expression":
                 # new Foo()
                 type_node = cursor.node.child_by_field_name("type")
                 if type_node:
                     type_name = self._get_text(type_node, None)
                     rels.append(
                         Relationship(
                            source_id=source_id,
                            target_id=f"ref::{type_name}",
                            kind=RelationshipKind.CALLS # Constructor call
                         )
                     )

            # Traverse
            if not visited_children and cursor.goto_first_child():
                visited_children = False
                continue
            
            if cursor.goto_next_sibling():
                visited_children = False
                continue
            
            if cursor.goto_parent():
                visited_children = True
                if cursor.node == node:
                    break
                continue
            else:
                break
                
        return rels
