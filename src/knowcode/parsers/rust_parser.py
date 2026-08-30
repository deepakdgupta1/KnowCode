"""Rust parser using Tree-sitter."""

from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.parsers.base import TreeSitterParser
from knowcode.parsers.rust_identity import (
    IMPLEMENTABLE_TYPES,
    LANGUAGE,
    TRAIT_TYPES,
    TYPE_DECLARATION_NODES,
    RustDeclaration,
    RustFileState,
    RustImplOwner,
    RustScope,
    qualified_segment,
    resolve_callee_endpoint,
    resolve_type_endpoint,
    strip_generics,
)
from knowcode.parsers.rust_syntax import (
    extract_attributes,
    extract_doc_comment,
    extract_function_signature,
    extract_visibility,
    parse_use_tree,
)
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
)


class RustParser(TreeSitterParser):
    """Parses Rust source files."""

    def __init__(self) -> None:
        """Initialize Rust parser."""
        super().__init__("rust")

    def _extract_file(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship], list[str]]:
        """Extract the whole file from its root module scope."""
        state = RustFileState(file_path=file_path)
        scope = self._build_scope(node, parent_id, self._module_scope(file_path))
        entities, relationships = self._extract_scope(
            node, file_path, scope, state, source_lines
        )
        return entities, relationships, state.errors

    def _build_scope(
        self,
        node: Any,
        parent_id: str,
        qualified_name: str,
    ) -> RustScope:
        """Pre-scan one module scope so references resolve order-independently.

        A name declared more than once in the scope is left out entirely: the
        program is ambiguous there, and picking either declaration would be a
        scan-order guess.
        """
        type_counts: dict[str, int] = {}
        declarations: dict[str, RustDeclaration] = {}
        function_counts: dict[str, int] = {}
        methods: dict[str, dict[str, str]] = {}

        for child in node.children:
            rust_type = TYPE_DECLARATION_NODES.get(child.type)
            if rust_type is not None:
                name = self._child_name(child)
                if name:
                    type_counts[name] = type_counts.get(name, 0) + 1
                    declarations[name] = RustDeclaration(
                        name=name,
                        qualified_name=f"{qualified_name}.{name}",
                        rust_type=rust_type,
                    )
            elif child.type == "function_item":
                name = self._child_name(child)
                if name:
                    function_counts[name] = function_counts.get(name, 0) + 1
            elif child.type == "impl_item":
                self._collect_impl_methods(child, qualified_name, methods)

        return RustScope(
            parent_id=parent_id,
            qualified_name=qualified_name,
            types={
                name: declaration
                for name, declaration in declarations.items()
                if type_counts[name] == 1
            },
            functions={
                name: f"{qualified_name}.{name}"
                for name, count in function_counts.items()
                if count == 1
            },
            methods=methods,
        )

    def _collect_impl_methods(
        self,
        node: Any,
        scope_qualified_name: str,
        methods: dict[str, dict[str, str]],
    ) -> None:
        """Record the methods one impl block contributes to its type."""
        type_node = node.child_by_field_name("type")
        body_node = node.child_by_field_name("body")
        if type_node is None or body_node is None:
            return

        type_text = self._get_text(type_node)
        base_type = strip_generics(type_text)
        segment = qualified_segment(type_text)
        if not base_type:
            return

        for child in body_node.children:
            if child.type != "function_item":
                continue
            name = self._child_name(child)
            if name:
                methods.setdefault(base_type, {})[name] = (
                    f"{scope_qualified_name}.{segment}.{name}"
                )

    def _child_name(self, node: Any) -> Optional[str]:
        """Return a declaration's name text when the grammar exposes one."""
        name_node = node.child_by_field_name("name")
        return self._get_text(name_node) if name_node is not None else None

    def _extract_scope(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract every declaration made directly in one module scope."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for child in node.children:
            child_type = child.type

            if child_type == "macro_invocation":
                macro_rel = self._parse_macro_invocation(child, file_path, scope)
                if macro_rel:
                    relationships.append(macro_rel)

            elif child_type == "struct_item":
                struct_entities, struct_rels = self._parse_struct(
                    child, file_path, scope, state, source_lines
                )
                entities.extend(struct_entities)
                relationships.extend(struct_rels)

            elif child_type in {"enum_item", "trait_item"}:
                type_entity, type_rels = self._parse_type_declaration(
                    child, file_path, scope, state, source_lines
                )
                if type_entity:
                    entities.append(type_entity)
                relationships.extend(type_rels)

            elif child_type == "impl_item":
                impl_entities, impl_rels = self._parse_impl(
                    child, file_path, scope, state, source_lines
                )
                entities.extend(impl_entities)
                relationships.extend(impl_rels)

            elif child_type == "function_item":
                func_entity, func_rels = self._parse_function(
                    child, file_path, scope, state, source_lines
                )
                if func_entity:
                    entities.append(func_entity)
                relationships.extend(func_rels)

            elif child_type in {"const_item", "static_item", "type_item"}:
                value_entity = self._parse_value_declaration(
                    child, file_path, scope, state, source_lines
                )
                if value_entity:
                    entities.append(value_entity)

            elif child_type == "mod_item":
                mod_entities, mod_rels = self._parse_module(
                    child, file_path, scope, state, source_lines
                )
                entities.extend(mod_entities)
                relationships.extend(mod_rels)

            elif child_type == "use_declaration":
                relationships.extend(self._parse_use_declaration(child, scope))

        return entities, relationships

    def _parse_struct(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse a struct declaration and its fields."""
        name = self._child_name(node)
        if not name:
            return [], []

        qualified_name = scope.qualify(name)
        if not state.claim(qualified_name, "struct"):
            return [], []

        struct_entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Rust structs map to CLASS
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=extract_doc_comment(node, source_lines),
        )
        self._apply_type_metadata(struct_entity, node, "struct", source_lines)

        entities = [struct_entity]
        relationships = [
            Relationship(
                source_id=scope.parent_id,
                target_id=struct_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        body_node = node.child_by_field_name("body")
        if body_node:
            field_entities, field_rels = self._parse_struct_fields(
                body_node,
                file_path,
                scope,
                state,
                source_lines,
                struct_entity,
                qualified_name,
            )
            entities.extend(field_entities)
            relationships.extend(field_rels)

        return entities, relationships

    def _parse_struct_fields(
        self,
        body_node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
        struct_entity: Entity,
        struct_qualified_name: str,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse the named fields of a struct body."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for field in body_node.children:
            if field.type != "field_declaration":
                continue

            field_name = self._child_name(field)
            if not field_name:
                continue

            field_qualified = f"{struct_qualified_name}.{field_name}"
            if not state.claim(field_qualified, "field"):
                continue

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

            field_type_node = field.child_by_field_name("type")
            if field_type_node is None:
                continue
            type_endpoint = resolve_type_endpoint(
                scope,
                file_path,
                self._get_text(field_type_node),
                field_qualified,
                IMPLEMENTABLE_TYPES,
            )
            if type_endpoint:
                relationships.append(
                    Relationship(
                        source_id=field_entity.id,
                        target_id=type_endpoint,
                        kind=RelationshipKind.USES_TYPE,
                    )
                )

        return entities, relationships

    def _parse_type_declaration(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> tuple[Optional[Entity], list[Relationship]]:
        """Parse an enum or trait declaration."""
        name = self._child_name(node)
        if not name:
            return None, []

        qualified_name = scope.qualify(name)
        rust_type = TYPE_DECLARATION_NODES[node.type]
        if not state.claim(qualified_name, rust_type):
            return None, []

        entity = self._create_entity(
            node,
            EntityKind.CLASS,  # Enums and traits map to CLASS
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=extract_doc_comment(node, source_lines),
        )
        self._apply_type_metadata(entity, node, rust_type, source_lines)

        return entity, [
            Relationship(
                source_id=scope.parent_id,
                target_id=entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

    def _apply_type_metadata(
        self,
        entity: Entity,
        node: Any,
        rust_type: str,
        source_lines: list[str],
    ) -> None:
        """Attach visibility, Rust kind, and attribute metadata to a type entity."""
        entity.metadata["visibility"] = extract_visibility(node)
        entity.metadata["rust_type"] = rust_type
        attributes = extract_attributes(node, source_lines)
        if attributes:
            entity.metadata["attributes"] = "|".join(attributes)

    def _parse_impl(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse an inherent or trait impl block.

        The implementing type owns its methods when it is declared in this
        scope. A foreign implementing type is not an entity of this graph, so
        it is recorded as method metadata instead of a fabricated edge source.
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        type_node = node.child_by_field_name("type")
        if type_node is None:
            return entities, relationships

        type_text = self._get_text(type_node)
        base_type = strip_generics(type_text)
        if not base_type:
            return entities, relationships

        owner = self._build_impl_owner(scope, file_path, type_text, base_type)
        trait_node = node.child_by_field_name("trait")
        trait_text = self._get_text(trait_node) if trait_node is not None else ""
        trait_name = strip_generics(trait_text)

        if owner.entity_id and trait_name:
            trait_endpoint = resolve_type_endpoint(
                scope, file_path, trait_text, scope.qualify(owner.segment), TRAIT_TYPES
            )
            if trait_endpoint:
                relationships.append(
                    Relationship(
                        source_id=owner.entity_id,
                        target_id=trait_endpoint,
                        kind=RelationshipKind.IMPLEMENTS,
                    )
                )

        body_node = node.child_by_field_name("body")
        if body_node is None:
            return entities, relationships

        for child in body_node.children:
            if child.type != "function_item":
                continue

            method_entity, method_rels = self._parse_function(
                child,
                file_path,
                scope,
                state,
                source_lines,
                owner=owner,
            )
            if method_entity is None:
                continue

            entities.append(method_entity)
            relationships.extend(method_rels)
            relationships.extend(
                self._annotate_impl_method(
                    method_entity, scope, file_path, owner, type_text, trait_text
                )
            )

        return entities, relationships

    def _build_impl_owner(
        self,
        scope: RustScope,
        file_path: Path,
        type_text: str,
        base_type: str,
    ) -> RustImplOwner:
        """Describe the type an impl block's methods belong to."""
        declaration = scope.types.get(base_type) if base_type.isidentifier() else None
        entity_id = (
            build_internal_entity_id(file_path, declaration.qualified_name)
            if declaration is not None and declaration.rust_type in IMPLEMENTABLE_TYPES
            else None
        )
        return RustImplOwner(
            type_name=base_type,
            segment=qualified_segment(type_text),
            entity_id=entity_id,
        )

    def _annotate_impl_method(
        self,
        method_entity: Entity,
        scope: RustScope,
        file_path: Path,
        owner: RustImplOwner,
        type_text: str,
        trait_text: str,
    ) -> list[Relationship]:
        """Record impl metadata on a method and link it to its trait."""
        trait_name = strip_generics(trait_text)
        method_entity.metadata["impl_type"] = "trait" if trait_name else "inherent"
        method_entity.metadata["associated_type"] = owner.type_name
        type_endpoint = resolve_type_endpoint(
            scope,
            file_path,
            type_text,
            method_entity.qualified_name,
            IMPLEMENTABLE_TYPES,
        )
        if type_endpoint:
            method_entity.metadata["associated_type_endpoint"] = type_endpoint

        if not trait_name:
            return []

        method_entity.metadata["implemented_trait"] = trait_name
        trait_endpoint = resolve_type_endpoint(
            scope, file_path, trait_text, method_entity.qualified_name, TRAIT_TYPES
        )
        if not trait_endpoint:
            return []
        return [
            Relationship(
                source_id=method_entity.id,
                target_id=trait_endpoint,
                kind=RelationshipKind.IMPLEMENTS,
            )
        ]

    def _parse_function(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
        owner: Optional[RustImplOwner] = None,
    ) -> tuple[Optional[Entity], list[Relationship]]:
        """Parse a free function or an impl method."""
        name = self._child_name(node)
        if not name:
            return None, []

        if owner is not None:
            qualified_name = scope.qualify(f"{owner.segment}.{name}")
            parent_id = owner.entity_id or scope.parent_id
        else:
            qualified_name = scope.qualify(name)
            parent_id = scope.parent_id

        if not state.claim(qualified_name, "method" if owner else "function"):
            return None, []

        self_type = owner.type_name if owner is not None else None
        func_entity = self._create_entity(
            node,
            EntityKind.FUNCTION,
            name,
            qualified_name,
            file_path,
            source_lines,
            docstring=extract_doc_comment(node, source_lines),
            signature=extract_function_signature(node, self_type),
        )

        func_entity.metadata["visibility"] = extract_visibility(node)
        func_entity.metadata["is_method"] = "true" if owner is not None else "false"

        attributes = extract_attributes(node, source_lines)
        if attributes:
            func_entity.metadata["attributes"] = "|".join(attributes)
            if any(
                "#[test]" in attr or "#[tokio::test]" in attr or "#[async_test]" in attr
                for attr in attributes
            ):
                func_entity.metadata["is_test"] = "true"

        relationships = [
            Relationship(
                source_id=parent_id,
                target_id=func_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        body_node = node.child_by_field_name("body")
        if body_node:
            relationships.extend(
                self._extract_calls_from_body(
                    body_node, func_entity.id, qualified_name, scope, file_path
                )
            )

        return func_entity, relationships

    def _parse_value_declaration(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> Optional[Entity]:
        """Parse a const, static, or type alias declaration."""
        name = self._child_name(node)
        if not name:
            return None

        qualified_name = scope.qualify(name)
        is_type_alias = node.type == "type_item"
        declaration_kind = {
            "const_item": "const",
            "static_item": "static",
            "type_item": "type_alias",
        }[node.type]
        if not state.claim(qualified_name, declaration_kind):
            return None

        entity = self._create_entity(
            node,
            EntityKind.CLASS if is_type_alias else EntityKind.VARIABLE,
            name,
            qualified_name,
            file_path,
            source_lines,
        )

        entity.metadata["visibility"] = extract_visibility(node)
        if is_type_alias:
            entity.metadata["rust_type"] = "type_alias"
        elif node.type == "const_item":
            entity.metadata["is_const"] = "true"
        else:
            entity.metadata["is_static"] = "true"

        return entity

    def _parse_module(
        self,
        node: Any,
        file_path: Path,
        scope: RustScope,
        state: RustFileState,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse an inline (``mod utils { ... }``) or external (``mod utils;``) module."""
        name = self._child_name(node)
        if not name:
            return [], []

        qualified_name = scope.qualify(name)
        if not state.claim(qualified_name, "module"):
            return [], []

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
        mod_entity.metadata["visibility"] = extract_visibility(node)
        mod_entity.metadata["is_external"] = "true" if is_external else "false"

        relationships = [
            Relationship(
                source_id=scope.parent_id,
                target_id=mod_entity.id,
                kind=RelationshipKind.CONTAINS,
            )
        ]

        if is_external:
            # Signal where the scanner should look for the module's source file.
            current_dir = file_path.parent
            possible_paths = [
                current_dir / f"{name}.rs",
                current_dir / name / "mod.rs",
            ]
            mod_entity.metadata["possible_file_paths"] = "|".join(
                str(path) for path in possible_paths
            )
            return [mod_entity], relationships

        nested_scope = self._build_scope(body_node, mod_entity.id, qualified_name)
        nested_entities, nested_rels = self._extract_scope(
            body_node, file_path, nested_scope, state, source_lines
        )
        return [mod_entity, *nested_entities], relationships + nested_rels

    def _parse_use_declaration(self, node: Any, scope: RustScope) -> list[Relationship]:
        """Parse a use declaration into explicit external references."""
        use_tree = node.child_by_field_name("argument")
        if use_tree is None:
            return []

        return [
            Relationship(
                source_id=scope.parent_id,
                target_id=build_external_reference_id(self.language_name, path),
                kind=RelationshipKind.IMPORTS,
            )
            for path in parse_use_tree(use_tree)
        ]

    def _extract_calls_from_body(
        self,
        body_node: Any,
        caller_id: str,
        caller_qualified_name: str,
        scope: RustScope,
        file_path: Path,
    ) -> list[Relationship]:
        """Extract call relationships from a function body."""
        relationships: list[Relationship] = []

        def visit_node(node: Any) -> None:
            if node.type == "call_expression":
                function_node = node.child_by_field_name("function")
                if function_node is not None:
                    target_id = resolve_callee_endpoint(
                        scope,
                        file_path,
                        self._get_text(function_node),
                        caller_qualified_name,
                    )
                    if target_id:
                        relationships.append(
                            Relationship(
                                source_id=caller_id,
                                target_id=target_id,
                                kind=RelationshipKind.CALLS,
                            )
                        )

            for child in node.children:
                visit_node(child)

        visit_node(body_node)
        return relationships

    def _parse_macro_invocation(
        self, node: Any, file_path: Path, scope: RustScope
    ) -> Optional[Relationship]:
        """Parse a macro invocation (e.g., println!, vec!) into a reference."""
        macro_node = node.child_by_field_name("macro")
        if macro_node is None:
            return None

        macro_name = self._get_text(macro_node).strip()
        if not macro_name:
            return None

        return Relationship(
            source_id=scope.parent_id,
            target_id=build_unresolved_reference_id(
                LANGUAGE, file_path, scope.qualified_name, f"{macro_name}!"
            ),
            kind=RelationshipKind.CALLS,
            metadata={"rust_reference": "macro"},
        )
