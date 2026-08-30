"""Vue Single File Component parser using Tree-sitter."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Union, cast
import re

from knowcode.data_models import (
    Entity,
    EntityKind,
    Relationship,
    RelationshipKind,
    Location,
    ParseResult,
)
from knowcode.parsers.base import TreeSitterParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_imports import scan_imports
from knowcode.parsers.vue_object_scan import find_balanced_block, top_level_keys
from knowcode.parsers.vue_script_index import (
    TYPESCRIPT_LANGS,
    DeclarationSpan,
    VueScriptIndex,
)
from knowcode.parsers.vue_sections import VueSection, scan_sfc_sections
from knowcode.parsers.vue_symbols import (
    BindingCategory,
    ResolvedReference,
    VueBinding,
    VueSymbolTable,
)
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_internal_entity_id,
    dedupe_entities_by_id,
)


#: Component name used when a file stem yields no PascalCase name at all.
DEFAULT_COMPONENT_NAME = "AnonymousComponent"

#: Compiler macros whose destructured result is already extracted elsewhere.
_COMPILER_MACRO_INITIALIZER = re.compile(r"\s*(?:defineProps|defineEmits)\b")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    """Return ``values`` without duplicates, preserving first-seen order.

    Extraction helpers scan several patterns over the same script, so ordering
    must come from the source rather than from set iteration; parser output has
    to be identical across processes.
    """
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


@dataclass
class _ScriptContext:
    """Everything one script block needs to emit component-owned entities."""

    file_path: Path
    component_id: str
    component_qualified_name: str
    source_lines: list[str]
    symbols: VueSymbolTable
    errors: list[str]
    index: VueScriptIndex


class VueParser(TreeSitterParser):
    """Parses Vue Single File Components (.vue files).

    Vue SFCs contain three sections:
    - <template>: HTML-like component template
    - <script>: JavaScript/TypeScript component logic
    - <style>: CSS/SCSS/Less styling
    """

    def __init__(self) -> None:
        """Initialize Vue parser.

        Note: tree-sitter-vue is not available in tree-sitter-languages,
        so we use regex-based parsing as the primary method.
        """
        # Don't call super().__init__ because vue language is not available
        self.language_name = "vue"
        # Script blocks are parsed with the real JS/TS grammars. Both parsers are
        # built once here and reused for every component.
        self.js_parser = JavaScriptParser()
        self.ts_parser = TypeScriptParser()
        self.parser = cast(Any, None)  # No tree-sitter parser for Vue
        self.language = None

    def parse_file(self, file_path: Union[str, Path]) -> ParseResult:
        """Parse a Vue SFC file.

        Override base method to handle Vue's multi-section structure.
        """
        file_path = Path(file_path)
        errors: list[str] = []

        try:
            source_code = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Failed to read file: {e}"],
            )

        # Scan top-level SFC blocks; malformed sections are reported, not dropped.
        scan = scan_sfc_sections(source_code)
        errors.extend(scan.errors)
        template_section = scan.first("template")
        script_section = scan.script()
        style_section = scan.first("style")

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        source_lines = source_code.splitlines()

        # The file itself, so its template, imports and top-of-file text have
        # an entity to hang on (BL-10). The component is a declaration inside
        # it and is scoped under it, which is what stops `Widget.vue` from
        # minting one id for both (BL-9, ADR 11).
        module_name = self._module_scope(file_path)
        module_id = build_internal_entity_id(file_path, module_name)
        module_entity = Entity(
            id=module_id,
            kind=EntityKind.MODULE,
            name=module_name,
            qualified_name=module_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(source_lines) or 1,
            ),
        )
        entities.append(module_entity)

        # Create component entity (the .vue file's default export)
        component_name = self._get_component_name(file_path)
        component_qualified_name = f"{module_name}.{component_name}"
        component_id = build_internal_entity_id(file_path, component_qualified_name)
        symbols = VueSymbolTable(file_path, component_qualified_name)

        # Determine Vue API type (composition vs options)
        vue_api_type = (
            "composition" if script_section and script_section.is_setup else "options"
        )

        component_entity = Entity(
            id=component_id,
            kind=EntityKind.CLASS,  # Vue components map to CLASS
            name=component_name,
            qualified_name=component_qualified_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(source_lines),
            ),
            metadata={
                "component_type": "vue_sfc",
                "vue_api": vue_api_type,
                "has_template": "true" if template_section else "false",
                "has_script": "true" if script_section else "false",
                "has_style": "true" if style_section else "false",
                "script_lang": (
                    (script_section.lang or "js") if script_section else "none"
                ),
            },
        )
        entities.append(component_entity)
        relationships.append(
            Relationship(
                source_id=module_id,
                target_id=component_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        # Parse component imports from every script block. A component may pair
        # a plain <script> with a <script setup>, and both can import. Imports
        # are collected first so composable calls know which names are foreign.
        seen_imports: set[tuple[str, RelationshipKind]] = set()
        for section in scan.all("script"):
            for relationship in self._extract_component_imports(
                section, component_id, symbols, errors
            ):
                key = (relationship.target_id, relationship.kind)
                if key in seen_imports:
                    continue
                seen_imports.add(key)
                relationships.append(relationship)

        # Vue merges a plain <script>'s Options API into a <script setup>
        # component, but only the setup block's declarations are indexed. Report
        # the companion block so its missing bindings are a visible limitation
        # rather than silent unresolved template references.
        skipped_scripts = [
            section for section in scan.all("script") if section is not script_section
        ]
        if skipped_scripts:
            errors.append(
                f"Indexed only the <script setup> block; {len(skipped_scripts)} "
                "companion <script> block(s) contributed imports but no "
                "declarations, so their template names may appear unresolved"
            )

        # Parse <script> section. This populates the component's symbol table,
        # so it must run before template and style references are resolved.
        if script_section:
            script_entities, script_rels = self._parse_script_section(
                script_section,
                file_path,
                component_id,
                component_qualified_name,
                source_lines,
                symbols,
                errors,
            )
            entities.extend(script_entities)
            relationships.extend(script_rels)

        # Parse <template> section
        if template_section:
            template_rels = self._parse_template_section(
                template_section,
                component_id,
                symbols,
            )
            relationships.extend(template_rels)

        # Parse <style> section for v-bind() CSS variable linking
        if style_section:
            style_rels = self._parse_style_section(
                style_section,
                component_id,
                symbols,
            )
            relationships.extend(style_rels)

        # Two declarations in one file cannot share one canonical ID; keep the
        # first and surface the dropped duplicate instead of letting it collapse
        # silently in GraphBuilder. Vue binding declarations already report
        # collisions through the symbol table; this is the final safety net for
        # any path (for example duplicate Options API methods) that bypasses it.
        entities, dedupe_errors = dedupe_entities_by_id(entities)
        errors.extend(dedupe_errors)

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )

    def _locate(
        self,
        span: DeclarationSpan,
        file_path: Path,
        source_lines: list[str],
    ) -> tuple[Location, str]:
        """Build a ``.vue`` location and its source snippet for a declaration."""
        return (
            Location(
                file_path=str(file_path),
                line_start=span.line_start,
                line_end=span.line_end,
            ),
            "\n".join(source_lines[span.line_start - 1 : span.line_end]),
        )

    def _add_entity(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        context: _ScriptContext,
        *,
        name: str,
        span: DeclarationSpan,
        kind: EntityKind,
        category: BindingCategory,
        metadata: dict[str, str],
        namespace: str = "",
    ) -> None:
        """Append one component-owned entity and its containment edge.

        ``namespace`` separates declarations that are not template bindings.
        Emitted events share a component with methods and refs but live in their
        own name space, so ``emits.`` keeps ``defineEmits(['save'])`` from
        colliding with a ``save()`` handler.

        A repeated template binding is a component defect that Vue itself
        rejects. It is reported through the parse result rather than producing a
        second entity under the same canonical ID.
        """
        qualified_name = f"{context.component_qualified_name}.{namespace}{name}"
        entity_id = build_internal_entity_id(context.file_path, qualified_name)
        binding = VueBinding(
            name=name,
            entity_id=entity_id,
            kind=kind,
            category=category,
            namespace=namespace,
        )
        if not context.symbols.declare(binding):
            context.errors.append(
                f"Duplicate Vue declaration {qualified_name!r}; "
                "keeping the first declaration"
            )
            return

        location, snippet = self._locate(span, context.file_path, context.source_lines)
        entities.append(
            Entity(
                id=entity_id,
                kind=kind,
                name=name,
                qualified_name=qualified_name,
                location=location,
                source_code=snippet,
                metadata=metadata,
            )
        )
        relationships.append(
            Relationship(
                source_id=context.component_id,
                target_id=entity_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

    def _reference_edge(
        self,
        component_id: str,
        reference: ResolvedReference,
        kind: RelationshipKind,
        binding_type: str,
        symbol: str,
        symbols: VueSymbolTable,
    ) -> Relationship:
        """Build one template or style edge and record how it was resolved."""
        metadata = {"binding_type": binding_type}
        if reference.category is not None:
            metadata["binding_category"] = reference.category.value
        if reference.resolution != "resolved":
            metadata["resolution"] = reference.resolution
            metadata["symbol"] = symbol
            module = symbols.import_module_for(symbol)
            if module is not None:
                metadata["import_path"] = module
        return Relationship(
            source_id=component_id,
            target_id=reference.target_id,
            kind=kind,
            metadata=metadata,
        )

    def _get_component_name(self, file_path: Path) -> str:
        """Get component name from file name (PascalCase convention).

        A stem made only of separators, such as the Nuxt catch-all route
        ``pages/_.vue``, leaves nothing to capitalize. Since the component name
        becomes a canonical entity ID, which cannot be empty, such a file falls
        back to a fixed name rather than aborting the parse.
        """
        # Convert kebab-case or snake_case to PascalCase
        name = file_path.stem
        # Replace hyphens and underscores with spaces, title case, remove spaces
        name = name.replace("-", " ").replace("_", " ")
        name = "".join(word.capitalize() for word in name.split())
        return name or DEFAULT_COMPONENT_NAME

    def _parse_script_section(
        self,
        script_section: VueSection,
        file_path: Path,
        parent_id: str,
        component_qualified_name: str,
        source_lines: list[str],
        symbols: VueSymbolTable,
        errors: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse the <script> section of a Vue component."""
        script_content = script_section.content
        lang = (script_section.lang or "").lower()
        context = _ScriptContext(
            file_path=file_path,
            component_id=parent_id,
            component_qualified_name=component_qualified_name,
            source_lines=source_lines,
            symbols=symbols,
            errors=errors,
            index=VueScriptIndex(
                script_content,
                self.ts_parser if lang in TYPESCRIPT_LANGS else self.js_parser,
                script_section.content_line_start,
            ),
        )

        parse = (
            self._parse_composition_api
            if script_section.is_setup
            else self._parse_options_api
        )
        return parse(script_content, context)

    def _parse_composition_api(
        self,
        script_content: str,
        context: _ScriptContext,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse <script setup> using Composition API."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []
        index = context.index

        # Extract defineProps
        props = self._extract_define_props(script_content)
        for prop_name in props:
            span = (
                index.lookup(prop_name, prefer_member=True)
                or index.literal(prop_name)
                or index.call("defineProps")
                or index.fallback()
            )
            self._add_entity(
                entities,
                relationships,
                context,
                name=prop_name,
                span=span,
                kind=EntityKind.VARIABLE,
                category=BindingCategory.PROP,
                metadata={"vue_api": "composition", "declaration_type": "prop"},
            )

        # Extract defineEmits
        emits = self._extract_define_emits(script_content)
        for emit_name in emits:
            span = (
                index.literal(emit_name)
                or index.call("defineEmits")
                or index.fallback()
            )
            self._add_entity(
                entities,
                relationships,
                context,
                name=emit_name,
                span=span,
                kind=EntityKind.VARIABLE,
                category=BindingCategory.SETUP,
                metadata={"vue_api": "composition", "declaration_type": "emit"},
                namespace="emits.",
            )

        # Extract ALL top-level declarations (comprehensive)
        all_declarations = self._extract_all_setup_declarations(script_content)
        for decl_name, decl_type in all_declarations:
            # Determine if this is a reactive declaration
            is_reactive = False
            is_arrow_function = False

            if decl_type in ["const", "let", "var"]:
                # Identifiers may contain '$', so the name must never be treated
                # as regex syntax.
                escaped_name = re.escape(decl_name)

                # Check if it's a ref/reactive call
                ref_check = re.search(
                    rf"{escaped_name}\s*=\s*(?:ref|reactive)\s*(?:<[^>]+>)?\s*\(",
                    script_content,
                )
                is_reactive = ref_check is not None

                # Check if it's an arrow function: const foo = () => {} or const bar = async () => {}
                arrow_check = re.search(
                    rf"{escaped_name}\s*=\s*(?:async\s+)?\([^)]*\)\s*=>", script_content
                )
                is_arrow_function = arrow_check is not None

            # Determine entity kind
            # Arrow functions should be treated as FUNCTION entities, not VARIABLE
            if decl_type == "function" or is_arrow_function:
                entity_kind = EntityKind.FUNCTION
                # Override decl_type for arrow functions
                if is_arrow_function:
                    decl_type = "function"
            else:
                entity_kind = EntityKind.VARIABLE

            self._add_entity(
                entities,
                relationships,
                context,
                name=decl_name,
                span=index.lookup(decl_name) or index.fallback(),
                kind=entity_kind,
                category=BindingCategory.SETUP,
                metadata={
                    "vue_api": "composition",
                    "declaration_type": decl_type,
                    "is_reactive": "true" if is_reactive else "false",
                    "exposed_to_template": "true",
                },
            )

        # Extract composables (useXxx). A component may define its own, so these
        # resolve against the symbol table before falling back to external.
        # Calling one twice is one dependency, not two.
        for composable in _ordered_unique(self._extract_composables(script_content)):
            relationships.append(
                self._reference_edge(
                    context.component_id,
                    context.symbols.resolve_call(composable),
                    RelationshipKind.CALLS,
                    "composable",
                    composable,
                    context.symbols,
                )
            )

        return entities, relationships

    def _parse_options_api(
        self,
        script_content: str,
        context: _ScriptContext,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse Options API (Vue 2 style or Vue 3 with defineComponent)."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        index = context.index

        # Extract data properties
        for prop_name in self._extract_data_properties(script_content):
            self._add_entity(
                entities,
                relationships,
                context,
                name=prop_name,
                span=index.lookup(prop_name, prefer_member=True) or index.fallback(),
                kind=EntityKind.VARIABLE,
                category=BindingCategory.DATA,
                metadata={
                    "vue_api": "options",
                    "declaration_type": "data",
                    "is_reactive": "true",
                },
            )

        # Extract methods
        for method_name in self._extract_methods(script_content):
            self._add_entity(
                entities,
                relationships,
                context,
                name=method_name,
                span=index.lookup(method_name, prefer_member=True) or index.fallback(),
                kind=EntityKind.METHOD,
                category=BindingCategory.METHOD,
                metadata={"vue_api": "options", "declaration_type": "method"},
            )

        # Extract computed properties
        for computed_name in self._extract_computed_properties(script_content):
            self._add_entity(
                entities,
                relationships,
                context,
                name=computed_name,
                span=index.lookup(computed_name, prefer_member=True)
                or index.fallback(),
                kind=EntityKind.VARIABLE,
                category=BindingCategory.COMPUTED,
                metadata={
                    "vue_api": "options",
                    "declaration_type": "computed",
                    "is_reactive": "true",
                },
            )

        return entities, relationships

    def _parse_template_section(
        self,
        template_section: VueSection,
        parent_id: str,
        symbols: VueSymbolTable,
    ) -> list[Relationship]:
        """Parse the <template> section to extract event handlers and component usage."""
        relationships = []
        template_content = template_section.content

        # Extract @click, @input, @submit etc. (event handlers)
        for handler in _ordered_unique(self._extract_event_handlers(template_content)):
            relationships.append(
                self._reference_edge(
                    parent_id,
                    symbols.resolve(handler),
                    RelationshipKind.CALLS,
                    "event",
                    handler,
                    symbols,
                )
            )

        # Extract v-model bindings
        for model in _ordered_unique(self._extract_v_models(template_content)):
            relationships.append(
                self._reference_edge(
                    parent_id,
                    symbols.resolve(model),
                    RelationshipKind.REFERENCES,
                    "model",
                    model,
                    symbols,
                )
            )

        # Extract component usage from template (CRITICAL for UI component tree).
        # A rendered tag names another component; it stays an explicit external
        # reference until cross-file resolution exists.
        for component_name in self._extract_component_usage(template_content):
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=build_external_reference_id(
                        "vue_component", component_name
                    ),
                    kind=RelationshipKind.REFERENCES,
                    metadata={"usage_type": "template"},
                )
            )

        return relationships

    def _extract_component_imports(
        self,
        script_section: VueSection,
        parent_id: str,
        symbols: VueSymbolTable,
        errors: list[str],
    ) -> list[Relationship]:
        """Extract import edges from a script section.

        A path specifier such as ``'./Foo.vue'``, ``'./Foo'``, or
        ``'@/components'`` names project source, so each binding it introduces
        becomes an explicit external Vue-component reference that template usage
        can line up with. A bare specifier names a package, so the statement
        produces one external module reference instead. A type-only import names
        neither, so it contributes no component reference.
        """
        relationships = []

        for statement in scan_imports(script_section.content):
            if not statement.is_valid:
                errors.append("Ignored an import with an empty module specifier")
                continue

            for binding in statement.bindings:
                symbols.declare_import(binding, statement.module)

            if not statement.is_path_import:
                relationships.append(
                    Relationship(
                        source_id=parent_id,
                        target_id=build_external_reference_id("npm", statement.module),
                        kind=RelationshipKind.IMPORTS,
                        metadata={"import_path": statement.module},
                    )
                )
                continue

            if statement.is_type_only:
                continue

            for binding in statement.bindings:
                relationships.append(
                    Relationship(
                        source_id=parent_id,
                        target_id=build_external_reference_id("vue_component", binding),
                        kind=RelationshipKind.IMPORTS,
                        metadata={"import_path": statement.module},
                    )
                )

        return relationships

    # Helper methods for extracting patterns

    def _extract_define_props(self, script_content: str) -> list[str]:
        """Extract props from defineProps().

        Handles:
        - Inline object: defineProps({ foo: String, bar: Number })
        - TypeScript inline: defineProps<{ foo: string, bar: number }>()
        - Multi-line TypeScript interface:
            defineProps<{
              foo: string
              bar: number
            }>()
        - External interface: defineProps<MyProps>() (just extracts "MyProps")
        """
        props = []

        # Pattern 1: Find defineProps call
        props_match = re.search(
            r"defineProps\s*(<[^>]*>)?\s*\(([^)]*)\)", script_content, re.DOTALL
        )

        if not props_match:
            # Try to match with bracket counting for complex cases
            props_start = re.search(r"defineProps\s*[<({]", script_content)
            if props_start:
                start_pos = props_start.end() - 1
                # Determine delimiter
                delimiter = script_content[start_pos]
                close_delimiter = (
                    ">" if delimiter == "<" else ")" if delimiter == "(" else "}"
                )

                # Find matching closing delimiter
                bracket_count = 1
                end_pos = start_pos + 1
                while end_pos < len(script_content) and bracket_count > 0:
                    if script_content[end_pos] == delimiter:
                        bracket_count += 1
                    elif script_content[end_pos] == close_delimiter:
                        bracket_count -= 1
                    end_pos += 1

                if bracket_count == 0:
                    props_text = script_content[start_pos:end_pos]
                    # Extract property names
                    prop_names = re.findall(
                        r"^\s*(\w+)\s*[?:]", props_text, re.MULTILINE
                    )
                    props.extend(prop_names)

        else:
            # Simple case - extract from matched text
            type_param = props_match.group(1)  # TypeScript generic part
            runtime_param = props_match.group(2)  # Runtime object part

            # Try TypeScript generic first
            if type_param:
                # Remove angle brackets
                type_text = type_param[1:-1].strip()
                # Check if it's an inline type definition or external interface
                if "{" in type_text:
                    # Inline type: { foo: string, bar: number }
                    prop_names = re.findall(
                        r"^\s*(\w+)\s*[?:]", type_text, re.MULTILINE
                    )
                    props.extend(prop_names)
                else:
                    # External interface: just return the interface name
                    # Note: This is less useful but we still track it
                    pass

            # Also check runtime object. Only its own keys are props: the keys
            # of a nested option object such as { type: String, default: 'x' }
            # describe one prop, they are not further props.
            if runtime_param and runtime_param.strip():
                brace = runtime_param.find("{")
                if brace == -1:
                    props.extend(top_level_keys(runtime_param))
                else:
                    body = find_balanced_block(runtime_param, brace)
                    props.extend(top_level_keys(body if body is not None else ""))

        return _ordered_unique(props)

    def _extract_define_emits(self, script_content: str) -> list[str]:
        """Extract emits from defineEmits()."""
        emits = []
        # Match: defineEmits<{ (e: 'update', value: string): void }>()
        # Or: defineEmits(['update', 'close'])
        emits_match = re.search(r"defineEmits\s*[<([]\s*[\s\S]*?[>)\]]", script_content)
        if emits_match:
            emits_text = emits_match.group(0)
            # Extract event names
            event_names = re.findall(r"['\"](\w+)['\"]", emits_text)
            emits.extend(event_names)
        return _ordered_unique(emits)

    def _extract_all_setup_declarations(
        self, script_content: str
    ) -> list[tuple[str, str]]:
        """Extract ALL top-level declarations in <script setup>.

        In Composition API with <script setup>, ALL top-level declarations
        are automatically exposed to the template.

        Returns:
            List of (name, declaration_type) tuples where declaration_type is:
            - 'const', 'let', 'var', 'function', 'class', 'interface', 'type'
        """
        declarations = []

        # Pattern 1: const/let/var declarations
        # Matches: const foo = ..., let bar = ..., var baz = ...
        # Handle destructuring: const { x, y } = ..., const [a, b] = ...
        variable_pattern = (
            r"(?:^|\n)\s*(const|let|var)\s+([a-zA-Z_$][\w$]*|\{[^}]+\}|\[[^\]]+\])\s*="
        )
        for match in re.finditer(variable_pattern, script_content, re.MULTILINE):
            decl_type = match.group(1)
            var_names = match.group(2)
            # Handle destructuring
            if var_names.startswith("{") or var_names.startswith("["):
                # Vue 3.5 reactive props destructure binds the props themselves:
                # `const { label } = defineProps({ label: String })` declares one
                # binding, not a prop plus a separate const of the same name.
                if _COMPILER_MACRO_INITIALIZER.match(script_content, match.end()):
                    continue
                # Extract names from destructuring
                inner = var_names[1:-1]
                names = re.findall(r"([a-zA-Z_$][\w$]*)", inner)
                for name in names:
                    if name not in ["as", "const", "let", "var"]:  # Skip keywords
                        declarations.append((name, decl_type))
            else:
                declarations.append((var_names, decl_type))

        # Pattern 2: function declarations
        # Matches: function foo() {}, async function bar() {}
        function_pattern = r"(?:^|\n)\s*(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*\("
        for match in re.finditer(function_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(1), "function"))

        # Pattern 3: arrow function assignments (already covered by variable_pattern but add type info)
        # Matches: const foo = () => {}, const bar = async () => {}
        arrow_function_pattern = (
            r"(?:^|\n)\s*const\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>"
        )
        for match in re.finditer(arrow_function_pattern, script_content, re.MULTILINE):
            name = match.group(1)
            # Update type to function if already in list
            declarations = [
                (n, "function" if n == name else t) for n, t in declarations
            ]

        # Pattern 4: class declarations
        # Matches: class Foo {}
        class_pattern = r"(?:^|\n)\s*class\s+([a-zA-Z_$][\w$]*)\s*[\s{]"
        for match in re.finditer(class_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(1), "class"))

        # Pattern 5: TypeScript type declarations
        # Matches: interface Foo {}, type Bar = ...
        type_pattern = r"(?:^|\n)\s*(interface|type)\s+([a-zA-Z_$][\w$]*)\s*[={]"
        for match in re.finditer(type_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(2), match.group(1)))

        # Several patterns can match the same name; the first classification of a
        # name wins so one declaration never produces two entities.
        seen: dict[str, str] = {}
        for name, decl_type in declarations:
            seen.setdefault(name, decl_type)
        return list(seen.items())

    def _extract_composables(self, script_content: str) -> list[str]:
        """Extract composable function calls (useXxx)."""
        composables = []
        # Match: useRouter(), useStore(), useCustom()
        composable_pattern = r"(use[A-Z]\w+)\s*\("
        for match in re.finditer(composable_pattern, script_content):
            composables.append(match.group(1))
        return composables

    def _option_block_keys(self, script_content: str, option: str) -> list[str]:
        """Return the top-level keys of an Options API block such as ``methods``."""
        start = re.search(rf"\b{option}\s*:\s*\{{", script_content)
        if start is None:
            return []
        body = find_balanced_block(script_content, start.end() - 1)
        return [] if body is None else _ordered_unique(top_level_keys(body))

    def _extract_data_properties(self, script_content: str) -> list[str]:
        """Extract data properties from Options API.

        Matches ``data() { return { foo: '', bar: 0 } }``. Only the keys of the
        returned object itself are data properties; a nested object's keys are
        part of a property's value.
        """
        data_start = re.search(r"\bdata\s*\(\s*\)\s*\{", script_content)
        if data_start is None:
            return []
        data_body = find_balanced_block(script_content, data_start.end() - 1)
        if data_body is None:
            return []

        returned = re.search(r"\breturn\s*\{", data_body)
        if returned is None:
            return []
        returned_object = find_balanced_block(data_body, returned.end() - 1)
        return (
            []
            if returned_object is None
            else _ordered_unique(top_level_keys(returned_object))
        )

    def _extract_methods(self, script_content: str) -> list[str]:
        """Extract methods from Options API.

        Matches ``methods: { foo() {}, bar() {} }``. Statements inside a method
        body are nested, so ``if (...) {`` cannot be mistaken for a declaration.
        """
        return self._option_block_keys(script_content, "methods")

    def _extract_computed_properties(self, script_content: str) -> list[str]:
        """Extract computed properties from Options API.

        Matches ``computed: { foo() {}, bar: { get() {}, set() {} } }``. The
        ``get``/``set`` pair of a writable computed belongs to ``bar``, so only
        ``bar`` itself is a computed property.
        """
        return self._option_block_keys(script_content, "computed")

    def _extract_event_handlers(self, template_content: str) -> list[str]:
        """Extract event handlers from template (@click, v-on:click, etc.).

        Filters out inline expressions like @click="count++" or @click="value = true"
        to only capture actual method names.
        """
        handlers = []
        # Match: @click="handleClick" or v-on:submit="onSubmit"
        # Capture the full expression, not just the first word
        event_pattern = r"(?:@|v-on:)\w+\s*=\s*['\"]([^'\"]+)['\"]"
        for match in re.finditer(event_pattern, template_content):
            expression = match.group(1).strip()

            # Filter out inline expressions with operators
            # Common operators: ++, --, =, +=, -=, +, -, *, /, %, &&, ||, !, <, >, etc.
            if any(
                op in expression
                for op in [
                    "++",
                    "--",
                    "+=",
                    "-=",
                    "*=",
                    "/=",
                    "%=",
                    "&&",
                    "||",
                    "===",
                    "!==",
                    "==",
                    "!=",
                    "<=",
                    ">=",
                    "=",
                    "+",
                    "-",
                    "*",
                    "/",
                    "%",
                    "!",
                    "<",
                    ">",
                ]
            ):
                continue

            # Filter out expressions with parentheses (function calls with args)
            if "(" in expression and ")" in expression:
                # Extract function name before parentheses
                func_match = re.match(r"(\w+)\s*\(", expression)
                if func_match:
                    handlers.append(func_match.group(1))
                continue

            # Filter out expressions with dots (property access like item.show = true)
            if "." in expression:
                continue

            # Filter out $emit calls (these are emits, not method handlers)
            if expression.startswith("$emit"):
                continue

            # If it's a simple identifier (method name), capture it
            if re.match(r"^[a-zA-Z_$][\w$]*$", expression):
                handlers.append(expression)

        return handlers

    def _extract_v_models(self, template_content: str) -> list[str]:
        """Extract v-model bindings from template."""
        models = []
        # Match: v-model="foo" or v-model:value="bar"
        model_pattern = r"v-model(?::\w+)?\s*=\s*['\"](\w+)"
        for match in re.finditer(model_pattern, template_content):
            models.append(match.group(1))
        return models

    def _parse_style_section(
        self,
        style_section: VueSection,
        parent_id: str,
        symbols: VueSymbolTable,
    ) -> list[Relationship]:
        """Parse the <style> section to extract v-bind() CSS variable linking.

        Vue 3.2+ allows binding JS variables directly in CSS:
        ```vue
        <style scoped>
        .text { color: v-bind(themeColor); }
        </style>
        ```

        The bound name lives in the component's template scope, so it resolves
        against the same symbol table as ``@click`` and ``v-model``.
        """
        relationships = []
        style_content = style_section.content

        for var_name in _ordered_unique(self._extract_css_bindings(style_content)):
            relationships.append(
                self._reference_edge(
                    parent_id,
                    symbols.resolve(var_name),
                    RelationshipKind.REFERENCES,
                    "css_variable",
                    var_name,
                    symbols,
                )
            )

        return relationships

    def _extract_css_bindings(self, style_content: str) -> list[str]:
        """Extract v-bind() variable references from CSS.

        Matches: v-bind(themeColor), v-bind('primary-color'), v-bind("fontSize")
        """
        bindings = []
        # Pattern: v-bind(varName) or v-bind('varName') or v-bind("varName")
        binding_pattern = r"v-bind\s*\(\s*['\"]?([a-zA-Z_$][\w$-]*)['\"]?\s*\)"
        for match in re.finditer(binding_pattern, style_content):
            var_name = match.group(1)
            # Convert kebab-case to camelCase if needed (CSS allows kebab-case)
            if "-" in var_name:
                var_name = "".join(
                    word.capitalize() if i > 0 else word
                    for i, word in enumerate(var_name.split("-"))
                )
            bindings.append(var_name)
        return bindings

    def _extract_component_usage(self, template_content: str) -> list[str]:
        """Extract component usage from template.

        This is the MOST IMPORTANT relationship for building UI component trees.
        Detects components used in the template like <MyButton />, <my-button>, etc.

        Returns:
            List of component names in PascalCase
        """
        components: list[str] = []

        # Pattern 1: Self-closing components
        # Matches: <MyButton />, <MyButton/>, <my-button />
        self_closing_pattern = r"<([A-Z][a-zA-Z0-9]*)\s*[^>]*/>"
        for match in re.finditer(self_closing_pattern, template_content):
            components.append(match.group(1))

        # Pattern 2: Regular components (opening and closing tags)
        # Matches: <MyButton>...</MyButton>
        regular_pattern = r"<([A-Z][a-zA-Z0-9]*)\s*[^>]*>"
        for match in re.finditer(regular_pattern, template_content):
            component_name = match.group(1)
            # Skip native HTML elements (rough check - uppercase first letter suggests component)
            if component_name[0].isupper():
                components.append(component_name)

        # Pattern 3: Kebab-case components (Vue convention)
        # Matches: <my-button>, <user-profile>
        # Convert to PascalCase for consistency
        kebab_pattern = r"<([a-z]+(?:-[a-z]+)+)\s*[^>]*[/>]"
        for match in re.finditer(kebab_pattern, template_content):
            kebab_name = match.group(1)
            # Convert kebab-case to PascalCase
            pascal_name = "".join(word.capitalize() for word in kebab_name.split("-"))
            components.append(pascal_name)

        return _ordered_unique(components)
