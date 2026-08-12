"""TypeScript parser using Tree-sitter."""

from pathlib import Path
from typing import Any

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.javascript_parser import JavaScriptParser, LocalSymbols
from knowcode.utils.entity_identity import build_internal_entity_id


class TypeScriptParser(JavaScriptParser):
    """Parse JavaScript syntax plus TypeScript type declarations."""

    def __init__(self) -> None:
        """Initialize TypeScript parser."""
        super().__init__("typescript")

    def _language_declared_names(self, node: Any) -> list[str]:
        """Collect TypeScript names for order-independent local resolution."""
        if node.type not in {
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
        }:
            return []
        name_node = node.child_by_field_name("name")
        return [self._get_text(name_node)] if name_node is not None else []

    def _parse_language_declaration(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        local_symbols: LocalSymbols,
        fallback_name: str | None,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse a TypeScript-only declaration after common dispatch."""
        if node.type in {
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
        }:
            return self._parse_ts_type_like(
                node,
                file_path,
                parent_id,
                source_code,
                source_lines,
                local_symbols,
            )
        return [], []

    def _parse_ts_type_like(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        local_symbols: LocalSymbols,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse TypeScript interface, type alias, or enum declarations as CLASS entities."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        name_node = node.child_by_field_name("name")
        if not name_node:
            return [], []

        ts_name = self._get_text(name_node)
        qualified_name = ts_name
        ts_id = build_internal_entity_id(file_path, qualified_name)

        if node.type == "interface_declaration":
            for child in node.children:
                if child.type != "extends_type_clause":
                    continue
                for super_type in child.named_children:
                    relationships.append(
                        Relationship(
                            source_id=ts_id,
                            target_id=self._resolve_reference_target(
                                super_type,
                                file_path,
                                qualified_name,
                                local_symbols,
                            ),
                            kind=RelationshipKind.INHERITS,
                        )
                    )

        entity = self._create_entity(
            node, EntityKind.CLASS, ts_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        relationships.append(
            Relationship(source_id=parent_id, target_id=ts_id, kind=RelationshipKind.CONTAINS)
        )

        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                if child.type in ("method_signature", "method_definition"):
                    method_entity, method_rels = self._parse_function(
                        child,
                        file_path,
                        ts_id,
                        source_code,
                        source_lines,
                        kind=EntityKind.METHOD,
                        parent_name=ts_name,
                        local_symbols=local_symbols,
                    )
                    entities.append(method_entity)
                    relationships.extend(method_rels)

        return entities, relationships
