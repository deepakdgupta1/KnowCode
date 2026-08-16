"""JavaScript parser using Tree-sitter."""

from pathlib import Path
from typing import Any

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.base import TreeSitterParser
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
)


LocalSymbols = dict[str, tuple[str, ...]]


class JavaScriptParser(TreeSitterParser):
    """Parses JavaScript/TypeScript files."""

    def __init__(self, language_name: str = "javascript") -> None:
        """Initialize a parser for one JavaScript-family grammar."""
        super().__init__(language_name)

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract JavaScript-family declarations through one shared dispatcher."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []
        local_symbols = self._collect_local_symbols(node)

        for child in node.children:
            child_entities, child_relationships = self._dispatch_declaration(
                child,
                file_path,
                parent_id,
                source_code,
                source_lines,
                local_symbols,
            )
            entities.extend(child_entities)
            relationships.extend(child_relationships)

        return entities, relationships

    def _dispatch_declaration(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        local_symbols: LocalSymbols,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Dispatch one top-level declaration for both JS and TS grammars."""
        declaration, fallback_name = self._unwrap_export(node)
        if declaration is None:
            return [], []

        if declaration.type in {"class", "class_declaration"}:
            return self._parse_class(
                declaration,
                file_path,
                parent_id,
                source_code,
                source_lines,
                local_symbols,
                fallback_name=fallback_name,
            )

        if declaration.type in {"function", "function_declaration"}:
            entity, relationships = self._parse_function(
                declaration,
                file_path,
                parent_id,
                source_code,
                source_lines,
                kind=EntityKind.FUNCTION,
                fallback_name=fallback_name,
                local_symbols=local_symbols,
            )
            return [entity], relationships

        if declaration.type in {"variable_declaration", "lexical_declaration"}:
            return self._parse_variable_declaration(
                declaration,
                file_path,
                parent_id,
                source_code,
                source_lines,
                local_symbols,
            )

        if declaration.type == "import_statement":
            source_node = declaration.child_by_field_name("source")
            if source_node is None:
                return [], []
            module_name = self._get_text(source_node).strip("'\"")
            return [], [
                Relationship(
                    source_id=parent_id,
                    target_id=build_external_reference_id("npm", module_name),
                    kind=RelationshipKind.IMPORTS,
                )
            ]

        return self._parse_language_declaration(
            declaration,
            file_path,
            parent_id,
            source_code,
            source_lines,
            local_symbols,
            fallback_name,
        )

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
        """Allow a JavaScript-family subclass to handle language-only nodes."""
        return [], []

    def _unwrap_export(self, node: Any) -> tuple[Any | None, str | None]:
        """Return the declaration inside an export and its stable fallback name."""
        if node.type != "export_statement":
            return node, None

        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            return declaration, None

        value = node.child_by_field_name("value")
        if value is not None and value.type in {"class", "function"}:
            return value, "default_export"
        return None, None

    def _collect_local_symbols(self, node: Any) -> LocalSymbols:
        """Collect module declarations before emitting order-independent edges."""
        collected: dict[str, list[str]] = {}
        for child in node.children:
            declaration, fallback_name = self._unwrap_export(child)
            if declaration is None:
                continue
            for name in self._declared_names(declaration, fallback_name):
                collected.setdefault(name, []).append(name)

        return {
            name: tuple(qualified_names) for name, qualified_names in collected.items()
        }

    def _declared_names(
        self,
        node: Any,
        fallback_name: str | None,
    ) -> list[str]:
        """Return qualified names introduced by one module declaration."""
        if node.type in {
            "class",
            "class_declaration",
            "function",
            "function_declaration",
        }:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                return [self._get_text(name_node)]
            return [fallback_name] if fallback_name else []

        if node.type in {"variable_declaration", "lexical_declaration"}:
            names: list[str] = []
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value_node = declarator.child_by_field_name("value")
                if (
                    name_node is not None
                    and value_node is not None
                    and value_node.type in {"arrow_function", "function"}
                ):
                    names.append(self._get_text(name_node))
            return names

        return self._language_declared_names(node)

    def _language_declared_names(self, node: Any) -> list[str]:
        """Return names introduced by subclass-specific declarations."""
        return []

    def _parse_variable_declaration(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
        local_symbols: LocalSymbols,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract function-like entities assigned to variables."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for decl in node.children:
            if decl.type != "variable_declarator":
                continue

            var_name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            fallback_name = (
                self._get_text(var_name_node) if var_name_node else "anonymous"
            )
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
                    local_symbols=local_symbols,
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
                    local_symbols=local_symbols,
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
        local_symbols: LocalSymbols,
        fallback_name: str | None = None,
    ) -> tuple[list[Entity], list[Relationship]]:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        name_node = node.child_by_field_name("name")
        if name_node is None and fallback_name is None:
            return [], []

        class_name = (
            self._get_text(name_node) if name_node is not None else fallback_name
        )
        assert class_name is not None
        qualified_name = class_name
        class_id = build_internal_entity_id(file_path, qualified_name)

        superclass = self._find_superclass(node)
        if superclass is not None:
            relationships.append(
                Relationship(
                    source_id=class_id,
                    target_id=self._resolve_reference_target(
                        superclass,
                        file_path,
                        qualified_name,
                        local_symbols,
                    ),
                    kind=RelationshipKind.INHERITS,
                )
            )

        entity = self._create_entity(
            node, EntityKind.CLASS, class_name, qualified_name, file_path, source_lines
        )
        entities.append(entity)
        relationships.append(
            Relationship(
                source_id=parent_id, target_id=class_id, kind=RelationshipKind.CONTAINS
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                if child.type == "method_definition":
                    method_entity, method_rels = self._parse_function(
                        child,
                        file_path,
                        class_id,
                        source_code,
                        source_lines,
                        kind=EntityKind.METHOD,
                        parent_name=class_name,
                        local_symbols=local_symbols,
                    )
                    entities.append(method_entity)
                    relationships.extend(method_rels)

        return entities, relationships

    def _find_superclass(self, node: Any) -> Any | None:
        """Read an ``extends`` expression from the locked JS or TS grammar."""
        heritage = next(
            (child for child in node.children if child.type == "class_heritage"),
            None,
        )
        if heritage is None:
            return None

        for child in heritage.named_children:
            if child.type == "extends_clause":
                value = child.child_by_field_name("value")
                return value or (
                    child.named_children[0] if child.named_children else None
                )
            if child.type != "implements_clause":
                return child
        return None

    def _resolve_reference_target(
        self,
        node: Any,
        file_path: Path,
        scope: str,
        local_symbols: LocalSymbols,
    ) -> str:
        """Resolve a proven local bare name or emit a scoped unresolved ID."""
        symbol = self._get_text(node)
        if node.type in {"identifier", "type_identifier"}:
            candidates = local_symbols.get(symbol, ())
            if len(candidates) == 1:
                return build_internal_entity_id(file_path, candidates[0])
        return build_unresolved_reference_id(
            self.language_name,
            file_path,
            scope,
            symbol,
        )

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
        local_symbols: LocalSymbols | None = None,
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

        func_id = build_internal_entity_id(file_path, qualified_name)

        entity = self._create_entity(
            node, kind, name, qualified_name, file_path, source_lines
        )

        relationships = [
            Relationship(
                source_id=parent_id, target_id=func_id, kind=RelationshipKind.CONTAINS
            )
        ]

        # Extract calls from body
        body_node = node.child_by_field_name("body")
        if body_node:
            calls = self._walk_for_calls(
                body_node,
                func_id,
                file_path,
                qualified_name,
                local_symbols or {},
            )
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
        local_symbols: LocalSymbols | None = None,
    ) -> tuple[Entity, list[Relationship]]:
        name = (
            self._get_text(name_node) if name_node else (fallback_name or "anonymous")
        )
        func_id = build_internal_entity_id(file_path, name)

        entity = self._create_entity(
            node, EntityKind.FUNCTION, name, name, file_path, source_lines
        )

        relationships = [
            Relationship(
                source_id=parent_id, target_id=func_id, kind=RelationshipKind.CONTAINS
            )
        ]

        body_node = node.child_by_field_name("body")
        if body_node:
            calls = self._walk_for_calls(
                body_node,
                func_id,
                file_path,
                name,
                local_symbols or {},
            )
            relationships.extend(calls)

        return entity, relationships

    def _walk_for_calls(
        self,
        node: Any,
        source_id: str,
        file_path: Path,
        scope: str,
        local_symbols: LocalSymbols,
    ) -> list[Relationship]:
        """Walk a callable body and emit each call node exactly once."""
        relationships: list[Relationship] = []
        pending = [node]
        while pending:
            current = pending.pop()
            if current.type in {"call_expression", "new_expression"}:
                relationship = self._extract_call(
                    current,
                    source_id,
                    file_path,
                    scope,
                    local_symbols,
                )
                if relationship is not None:
                    relationships.append(relationship)
            pending.extend(reversed(current.named_children))

        return relationships

    def _extract_call(
        self,
        node: Any,
        source_id: str,
        file_path: Path,
        scope: str,
        local_symbols: LocalSymbols,
    ) -> Relationship | None:
        """Extract a function or constructor call with a classified target."""
        callable_node = node.child_by_field_name("function")
        if callable_node is None:
            callable_node = node.child_by_field_name("constructor")
        if callable_node is None:
            return None

        callee_name = self._get_text(callable_node)
        if callee_name == "require":
            arguments = node.child_by_field_name("arguments")
            if arguments is not None and arguments.named_child_count > 0:
                first_argument = arguments.named_child(0)
                if first_argument.type == "string":
                    module = self._get_text(first_argument).strip("'\"")
                    return Relationship(
                        source_id=source_id,
                        target_id=build_external_reference_id("npm", module),
                        kind=RelationshipKind.IMPORTS,
                    )

        return Relationship(
            source_id=source_id,
            target_id=self._resolve_reference_target(
                callable_node,
                file_path,
                scope,
                local_symbols,
            ),
            kind=RelationshipKind.CALLS,
        )
