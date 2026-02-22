"""TypeScript parser using Tree-sitter."""

from typing import Any
from pathlib import Path

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.javascript_parser import JavaScriptParser


class TypeScriptParser(JavaScriptParser):
    """Parses TypeScript files.
    
    Inherits from JavaScriptParser to reuse behavior for common JS syntax (functions, classes, etc.)
    and adds support for TypeScript-specific syntax like interface, type, and enum declarations.
    """

    def __init__(self) -> None:
        """Initialize TypeScript parser."""
        # Using the base parser initialization logic but with the "typescript" grammar
        super(JavaScriptParser, self).__init__("typescript")

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract entities from TypeScript AST."""
        
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Let the base JavaScript parser handle common statements
        # However, because super()._extract_entities recursively walks the children,
        # we can't just call it easily without losing TS nodes, since it skips unknown nodes.
        # Instead, we reimplement the child traversal loop here, calling TS-specific extractors
        # or falling back to the JS extractors for known elements.

        for child in node.children:
            child_type = child.type
            
            # TypeScript specific structures
            if child_type in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
                ts_entities, ts_rels = self._parse_ts_type_like(
                    child, file_path, parent_id, source_code, source_lines
                )
                entities.extend(ts_entities)
                relationships.extend(ts_rels)

            # Standard JavaScript structures Supported by Base Parser
            elif child_type == "class_declaration":
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
                
            elif child_type in ("variable_declaration", "lexical_declaration"):
                 for decl in child.children:
                     if decl.type == "variable_declarator":
                         var_name_node = decl.child_by_field_name("name")
                         value_node = decl.child_by_field_name("value")
                         
                         if value_node and value_node.type == "arrow_function":
                             func_entity, func_rels = self._parse_arrow_function(
                                 value_node, var_name_node, file_path, parent_id, source_code, source_lines
                             )
                             entities.append(func_entity)
                             relationships.extend(func_rels)

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

    def _parse_ts_type_like(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse TypeScript interface, type alias, or enum declarations as CLASS entities."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        name_node = node.child_by_field_name("name")
        if not name_node:
            return [], []

        ts_name = self._get_text(name_node)
        qualified_name = ts_name
        ts_id = f"{file_path}::{qualified_name}"
        
        # Check for extends on interfaces
        if node.type == "interface_declaration":
             for child in node.children:
                 if child.type == "extends_clause":
                     # In TS, extends_clause children might be multiple type_identifiers
                     for super_type in child.children:
                          if super_type.type == "type_identifier":
                               base_name = self._get_text(super_type)
                               relationships.append(
                                  Relationship(
                                      source_id=ts_id,
                                      target_id=f"ref::{base_name}",
                                      kind=RelationshipKind.INHERITS
                                  )
                               )

        entity = self._create_entity(
            node, EntityKind.CLASS, ts_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        
        relationships.append(
            Relationship(source_id=parent_id, target_id=ts_id, kind=RelationshipKind.CONTAINS)
        )

        # Parse methods in an interface (body -> object_type -> method_signature)
        # or methods in a class
        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                # TS interfaces use method_signature, normal JS uses method_definition
                if child.type in ("method_signature", "method_definition"):
                    method_entity, method_rels = self._parse_function(
                        child, file_path, ts_id, source_code, source_lines, kind=EntityKind.METHOD, parent_name=ts_name
                    )
                    entities.append(method_entity)
                    relationships.extend(method_rels)

        return entities, relationships
