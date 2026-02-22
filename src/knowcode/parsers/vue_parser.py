"""Vue Single File Component parser using Tree-sitter."""

from pathlib import Path
from typing import Any, Union, cast
import re

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind, Location, ParseResult
from knowcode.parsers.base import TreeSitterParser
from knowcode.parsers.javascript_parser import JavaScriptParser


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
        self.js_parser = JavaScriptParser()
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

        # Extract sections using regex (fallback if tree-sitter fails)
        sections = self._extract_sections(source_code)

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        source_lines = source_code.splitlines()

        # Create component entity (the .vue file itself)
        component_name = self._get_component_name(file_path)
        component_id = f"{file_path}::{component_name}"

        # Determine Vue API type (composition vs options)
        vue_api_type = "composition" if sections.get("script", {}).get("is_setup") else "options"

        component_entity = Entity(
            id=component_id,
            kind=EntityKind.CLASS,  # Vue components map to CLASS
            name=component_name,
            qualified_name=component_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(source_lines),
            ),
            metadata={
                "component_type": "vue_sfc",
                "vue_api": vue_api_type,
                "has_template": "true" if sections.get("template") else "false",
                "has_script": "true" if sections.get("script") else "false",
                "has_style": "true" if sections.get("style") else "false",
                "script_lang": sections.get("script", {}).get("lang", "js") if sections.get("script") else "none",
            }
        )
        entities.append(component_entity)

        # Parse <script> section
        if sections.get("script"):
            script_entities, script_rels = self._parse_script_section(
                sections["script"],
                file_path,
                component_id,
                source_lines,
            )
            entities.extend(script_entities)
            relationships.extend(script_rels)

        # Parse <template> section
        if sections.get("template"):
            template_rels = self._parse_template_section(
                sections["template"],
                component_id,
            )
            relationships.extend(template_rels)

        # Parse component imports from script
        if sections.get("script"):
            import_rels = self._extract_component_imports(
                sections["script"],
                component_id,
            )
            relationships.extend(import_rels)

        # Parse <style> section for v-bind() CSS variable linking
        if sections.get("style"):
            style_rels = self._parse_style_section(
                sections["style"],
                component_id,
            )
            relationships.extend(style_rels)

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )

    def _extract_entities(
        self,
        node: Any,
        file_path: Path,
        parent_id: str,
        source_code: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract entities from Vue AST (tree-sitter mode).

        This is used if tree-sitter-vue is available.
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for child in node.children:
            child_type = child.type

            if child_type == "script_element":
                # Parse <script> section
                script_text = self._get_text(child)
                script_entities, script_rels = self._parse_script_content(
                    script_text, file_path, parent_id, source_lines
                )
                entities.extend(script_entities)
                relationships.extend(script_rels)

            elif child_type == "template_element":
                # Parse <template> section
                template_rels = self._parse_template_content(child, parent_id)
                relationships.extend(template_rels)

        return entities, relationships

    def _extract_sections(self, source_code: str) -> dict[str, dict[str, Any]]:
        """Extract <template>, <script>, and <style> sections using regex."""
        sections = {}

        # Extract <script> section
        script_match = re.search(
            r'<script\s*(?:setup)?\s*(?:lang="(ts|js)")?\s*>(.*?)</script>',
            source_code,
            re.DOTALL | re.IGNORECASE,
        )
        if script_match:
            lang = script_match.group(1) or "js"
            content = script_match.group(2)
            start_pos = script_match.start()
            # Count lines before script section
            line_start = source_code[:start_pos].count("\n") + 1
            sections["script"] = {
                "content": content,
                "lang": lang,
                "line_start": line_start,
                "is_setup": "setup" in script_match.group(0),
            }

        # Extract <template> section using tag counting (handles nested templates)
        template_start_match = re.search(
            r"<template\s*>",
            source_code,
            re.IGNORECASE,
        )
        if template_start_match:
            start_pos = template_start_match.end()
            line_start = source_code[:template_start_match.start()].count("\n") + 1

            # Use tag counting to find matching </template>
            tag_count = 1
            end_pos = start_pos
            i = start_pos
            while i < len(source_code) and tag_count > 0:
                # Look for next opening or closing template tag
                next_open = source_code.find("<template", i)
                next_close = source_code.find("</template>", i)

                if next_close == -1:
                    break

                if next_open != -1 and next_open < next_close:
                    # Found opening tag first
                    tag_count += 1
                    i = next_open + 9  # len("<template")
                else:
                    # Found closing tag
                    tag_count -= 1
                    if tag_count == 0:
                        end_pos = next_close
                        break
                    i = next_close + 11  # len("</template>")

            if end_pos > start_pos:
                content = source_code[start_pos:end_pos]
                sections["template"] = {
                    "content": content,
                    "line_start": line_start,
                }

        # Extract <style> section
        style_match = re.search(
            r'<style\s*(?:scoped)?\s*(?:lang="(css|scss|less)")?\s*>(.*?)</style>',
            source_code,
            re.DOTALL | re.IGNORECASE,
        )
        if style_match:
            lang = style_match.group(1) or "css"
            content = style_match.group(2)
            start_pos = style_match.start()
            line_start = source_code[:start_pos].count("\n") + 1
            sections["style"] = {
                "content": content,
                "lang": lang,
                "line_start": line_start,
            }

        return sections

    def _get_component_name(self, file_path: Path) -> str:
        """Get component name from file name (PascalCase convention)."""
        # Convert kebab-case or snake_case to PascalCase
        name = file_path.stem
        # Replace hyphens and underscores with spaces, title case, remove spaces
        name = name.replace("-", " ").replace("_", " ")
        name = "".join(word.capitalize() for word in name.split())
        return name

    def _parse_script_section(
        self,
        script_info: dict[str, Any],
        file_path: Path,
        parent_id: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse the <script> section of a Vue component."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        script_content = script_info["content"]
        is_setup = script_info.get("is_setup", False)

        if is_setup:
            # Parse <script setup> (Composition API)
            script_entities, script_rels = self._parse_composition_api(
                script_content, file_path, parent_id, source_lines, script_info["line_start"]
            )
            entities.extend(script_entities)
            relationships.extend(script_rels)
        else:
            # Parse Options API or regular script
            script_entities, script_rels = self._parse_options_api(
                script_content, file_path, parent_id, source_lines, script_info["line_start"]
            )
            entities.extend(script_entities)
            relationships.extend(script_rels)

        return entities, relationships

    def _parse_composition_api(
        self,
        script_content: str,
        file_path: Path,
        parent_id: str,
        source_lines: list[str],
        line_offset: int,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse <script setup> using Composition API."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Extract defineProps
        props = self._extract_define_props(script_content)
        for prop_name in props:
            prop_entity = Entity(
                id=f"{parent_id}::prop::{prop_name}",
                kind=EntityKind.VARIABLE,
                name=prop_name,
                qualified_name=f"{parent_id.split('::')[-1]}.props.{prop_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={"vue_api": "composition", "declaration_type": "prop"}
            )
            entities.append(prop_entity)
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=prop_entity.id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

        # Extract defineEmits
        emits = self._extract_define_emits(script_content)
        for emit_name in emits:
            emit_entity = Entity(
                id=f"{parent_id}::emit::{emit_name}",
                kind=EntityKind.VARIABLE,
                name=emit_name,
                qualified_name=f"{parent_id.split('::')[-1]}.emits.{emit_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={"vue_api": "composition", "declaration_type": "emit"}
            )
            entities.append(emit_entity)
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=emit_entity.id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

        # Extract ALL top-level declarations (comprehensive)
        all_declarations = self._extract_all_setup_declarations(script_content)
        for decl_name, decl_type in all_declarations:
            # Determine if this is a reactive declaration
            is_reactive = False
            is_arrow_function = False

            if decl_type in ['const', 'let', 'var']:
                # Check if it's a ref/reactive call
                ref_check = re.search(
                    rf"{decl_name}\s*=\s*(?:ref|reactive)\s*(?:<[^>]+>)?\s*\(",
                    script_content
                )
                is_reactive = ref_check is not None

                # Check if it's an arrow function: const foo = () => {} or const bar = async () => {}
                arrow_check = re.search(
                    rf"{decl_name}\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",
                    script_content
                )
                is_arrow_function = arrow_check is not None

            # Determine entity kind
            # Arrow functions should be treated as FUNCTION entities, not VARIABLE
            if decl_type == 'function' or is_arrow_function:
                entity_kind = EntityKind.FUNCTION
                # Override decl_type for arrow functions
                if is_arrow_function:
                    decl_type = 'function'
            else:
                entity_kind = EntityKind.VARIABLE

            # For backwards compatibility, use "ref" for reactive variables
            # Use "function" for arrow functions
            if is_reactive:
                id_type = "ref"
            elif is_arrow_function:
                id_type = "function"
            else:
                id_type = decl_type

            decl_entity = Entity(
                id=f"{parent_id}::{id_type}::{decl_name}",
                kind=entity_kind,
                name=decl_name,
                qualified_name=f"{parent_id.split('::')[-1]}.{decl_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={
                    "vue_api": "composition",
                    "declaration_type": decl_type,
                    "is_reactive": "true" if is_reactive else "false",
                    "exposed_to_template": "true"
                }
            )
            entities.append(decl_entity)
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=decl_entity.id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

        # Extract composables (useXxx)
        composables = self._extract_composables(script_content)
        for composable in composables:
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=f"composable::{composable}",
                    kind=RelationshipKind.CALLS,
                )
            )

        return entities, relationships

    def _parse_options_api(
        self,
        script_content: str,
        file_path: Path,
        parent_id: str,
        source_lines: list[str],
        line_offset: int,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse Options API (Vue 2 style or Vue 3 with defineComponent)."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Extract data properties
        data_props = self._extract_data_properties(script_content)
        for prop_name in data_props:
            prop_entity = Entity(
                id=f"{parent_id}::data::{prop_name}",
                kind=EntityKind.VARIABLE,
                name=prop_name,
                qualified_name=f"{parent_id.split('::')[-1]}.data.{prop_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={
                    "vue_api": "options",
                    "declaration_type": "data",
                    "is_reactive": "true"
                }
            )
            entities.append(prop_entity)

        # Extract methods
        methods = self._extract_methods(script_content)
        for method_name in methods:
            method_entity = Entity(
                id=f"{parent_id}::method::{method_name}",
                kind=EntityKind.FUNCTION,
                name=method_name,
                qualified_name=f"{parent_id.split('::')[-1]}.{method_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={
                    "vue_api": "options",
                    "declaration_type": "method"
                }
            )
            entities.append(method_entity)
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=method_entity.id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

        # Extract computed properties
        computed = self._extract_computed_properties(script_content)
        for computed_name in computed:
            computed_entity = Entity(
                id=f"{parent_id}::computed::{computed_name}",
                kind=EntityKind.VARIABLE,
                name=computed_name,
                qualified_name=f"{parent_id.split('::')[-1]}.computed.{computed_name}",
                location=Location(
                    file_path=str(file_path),
                    line_start=line_offset,
                    line_end=line_offset + 1,
                ),
                metadata={
                    "vue_api": "options",
                    "declaration_type": "computed",
                    "is_reactive": "true"
                }
            )
            entities.append(computed_entity)

        return entities, relationships

    def _parse_template_section(
        self, template_info: dict[str, Any], parent_id: str
    ) -> list[Relationship]:
        """Parse the <template> section to extract event handlers and component usage."""
        relationships = []
        template_content = template_info["content"]

        # Extract @click, @input, @submit etc. (event handlers)
        event_handlers = self._extract_event_handlers(template_content)
        for handler in event_handlers:
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=f"{parent_id}::method::{handler}",
                    kind=RelationshipKind.CALLS,
                )
            )

        # Extract v-model bindings
        models = self._extract_v_models(template_content)
        for model in models:
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=f"{parent_id}::data::{model}",
                    kind=RelationshipKind.REFERENCES,
                )
            )

        # Extract component usage from template (CRITICAL for UI component tree)
        component_usage = self._extract_component_usage(template_content)
        for component_name in component_usage:
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=f"vue_component::{component_name}",
                    kind=RelationshipKind.REFERENCES,
                    metadata={"usage_type": "template"}
                )
            )

        return relationships

    def _extract_component_imports(
        self, script_info: dict[str, Any], parent_id: str
    ) -> list[Relationship]:
        """Extract component imports from script section.

        Handles:
        - Extension-less imports: import Foo from './Foo'
        - .vue extensions: import Foo from './Foo.vue'
        - Alias imports: import Foo from '@/components/Foo'
        - Multiple imports: import { Foo, Bar } from './components'
        """
        relationships = []
        script_content = script_info["content"]

        # Pattern 1: Default imports with optional .vue extension
        # Matches: import Foo from './Foo', import Foo from './Foo.vue', import Foo from '@/components/Foo'
        default_import_pattern = r"import\s+(\w+)\s+from\s+['\"]([^'\"]+?)(?:\.vue)?['\"]"
        for match in re.finditer(default_import_pattern, script_content):
            component_name = match.group(1)
            component_path = match.group(2)
            # Only create relationship if path looks like a component (starts with . or @)
            if component_path.startswith('.') or component_path.startswith('@'):
                relationships.append(
                    Relationship(
                        source_id=parent_id,
                        target_id=f"vue_component::{component_name}",
                        kind=RelationshipKind.IMPORTS,
                        metadata={"import_path": component_path}
                    )
                )

        # Pattern 2: Named imports (destructured)
        # Matches: import { Foo, Bar } from './components'
        named_import_pattern = r"import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]"
        for match in re.finditer(named_import_pattern, script_content):
            component_names_str = match.group(1)
            component_path = match.group(2)
            # Extract individual component names
            component_names = [name.strip() for name in component_names_str.split(',')]
            for component_name in component_names:
                # Handle "as" aliasing: import { Foo as MyFoo }
                if ' as ' in component_name:
                    component_name = component_name.split(' as ')[-1].strip()
                if component_path.startswith('.') or component_path.startswith('@'):
                    relationships.append(
                        Relationship(
                            source_id=parent_id,
                            target_id=f"vue_component::{component_name}",
                            kind=RelationshipKind.IMPORTS,
                            metadata={"import_path": component_path}
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
            r"defineProps\s*(<[^>]*>)?\s*\(([^)]*)\)",
            script_content,
            re.DOTALL
        )

        if not props_match:
            # Try to match with bracket counting for complex cases
            props_start = re.search(r"defineProps\s*[<({]", script_content)
            if props_start:
                start_pos = props_start.end() - 1
                # Determine delimiter
                delimiter = script_content[start_pos]
                close_delimiter = '>' if delimiter == '<' else ')' if delimiter == '(' else '}'

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
                    prop_names = re.findall(r"^\s*(\w+)\s*[?:]", props_text, re.MULTILINE)
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
                if '{' in type_text:
                    # Inline type: { foo: string, bar: number }
                    prop_names = re.findall(r"^\s*(\w+)\s*[?:]", type_text, re.MULTILINE)
                    props.extend(prop_names)
                else:
                    # External interface: just return the interface name
                    # Note: This is less useful but we still track it
                    pass

            # Also check runtime object
            if runtime_param and runtime_param.strip():
                prop_names = re.findall(r"(\w+)\s*:", runtime_param)
                props.extend(prop_names)

        return list(set(props))  # Remove duplicates

    def _extract_define_emits(self, script_content: str) -> list[str]:
        """Extract emits from defineEmits()."""
        emits = []
        # Match: defineEmits<{ (e: 'update', value: string): void }>()
        # Or: defineEmits(['update', 'close'])
        emits_match = re.search(
            r"defineEmits\s*[<([]\s*[\s\S]*?[>)\]]", script_content
        )
        if emits_match:
            emits_text = emits_match.group(0)
            # Extract event names
            event_names = re.findall(r"['\"](\w+)['\"]", emits_text)
            emits.extend(event_names)
        return emits

    def _extract_refs(self, script_content: str) -> list[str]:
        """Extract ref() and reactive() declarations."""
        refs = []
        # Match: const foo = ref(...) or const bar = ref<Type>(...) or const baz = reactive(...)
        # Allow optional TypeScript generics: ref<string>(...)
        ref_pattern = r"const\s+(\w+)\s*=\s*(?:ref|reactive)\s*(?:<[^>]+>)?\s*\("
        for match in re.finditer(ref_pattern, script_content):
            refs.append(match.group(1))
        return refs

    def _extract_all_setup_declarations(self, script_content: str) -> list[tuple[str, str]]:
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
        variable_pattern = r"(?:^|\n)\s*(const|let|var)\s+([a-zA-Z_$][\w$]*|\{[^}]+\}|\[[^\]]+\])\s*="
        for match in re.finditer(variable_pattern, script_content, re.MULTILINE):
            decl_type = match.group(1)
            var_names = match.group(2)
            # Handle destructuring
            if var_names.startswith('{') or var_names.startswith('['):
                # Extract names from destructuring
                inner = var_names[1:-1]
                names = re.findall(r'([a-zA-Z_$][\w$]*)', inner)
                for name in names:
                    if name not in ['as', 'const', 'let', 'var']:  # Skip keywords
                        declarations.append((name, decl_type))
            else:
                declarations.append((var_names, decl_type))

        # Pattern 2: function declarations
        # Matches: function foo() {}, async function bar() {}
        function_pattern = r"(?:^|\n)\s*(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*\("
        for match in re.finditer(function_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(1), 'function'))

        # Pattern 3: arrow function assignments (already covered by variable_pattern but add type info)
        # Matches: const foo = () => {}, const bar = async () => {}
        arrow_function_pattern = r"(?:^|\n)\s*const\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>"
        for match in re.finditer(arrow_function_pattern, script_content, re.MULTILINE):
            name = match.group(1)
            # Update type to function if already in list
            declarations = [(n, 'function' if n == name else t) for n, t in declarations]

        # Pattern 4: class declarations
        # Matches: class Foo {}
        class_pattern = r"(?:^|\n)\s*class\s+([a-zA-Z_$][\w$]*)\s*[\s{]"
        for match in re.finditer(class_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(1), 'class'))

        # Pattern 5: TypeScript type declarations
        # Matches: interface Foo {}, type Bar = ...
        type_pattern = r"(?:^|\n)\s*(interface|type)\s+([a-zA-Z_$][\w$]*)\s*[={]"
        for match in re.finditer(type_pattern, script_content, re.MULTILINE):
            declarations.append((match.group(2), match.group(1)))

        return declarations

    def _extract_composables(self, script_content: str) -> list[str]:
        """Extract composable function calls (useXxx)."""
        composables = []
        # Match: useRouter(), useStore(), useCustom()
        composable_pattern = r"(use[A-Z]\w+)\s*\("
        for match in re.finditer(composable_pattern, script_content):
            composables.append(match.group(1))
        return composables

    def _extract_data_properties(self, script_content: str) -> list[str]:
        """Extract data properties from Options API."""
        data_props = []
        # Match: data() { return { foo: '', bar: 0 } }
        data_match = re.search(
            r"data\s*\(\s*\)\s*\{[\s\S]*?return\s*\{([\s\S]*?)\}", script_content
        )
        if data_match:
            data_obj = data_match.group(1)
            # Extract property names
            prop_names = re.findall(r"(\w+)\s*:", data_obj)
            data_props.extend(prop_names)
        return data_props

    def _extract_methods(self, script_content: str) -> list[str]:
        """Extract methods from Options API."""
        methods = []
        # Match: methods: { foo() {}, bar() {} }
        # Find the start of methods block
        methods_start = re.search(r"methods\s*:\s*\{", script_content)
        if methods_start:
            # Find matching closing brace using bracket counting
            start_pos = methods_start.end() - 1  # Position of opening {
            brace_count = 0
            end_pos = start_pos
            for i in range(start_pos, len(script_content)):
                if script_content[i] == '{':
                    brace_count += 1
                elif script_content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i
                        break

            if end_pos > start_pos:
                methods_obj = script_content[start_pos + 1:end_pos]
                # Extract method names
                method_names = re.findall(r"(\w+)\s*\([^)]*\)\s*\{", methods_obj)
                methods.extend(method_names)
        return methods

    def _extract_computed_properties(self, script_content: str) -> list[str]:
        """Extract computed properties from Options API."""
        computed = []
        # Match: computed: { foo() {}, bar: { get() {}, set() {} } }
        # Find the start of computed block
        computed_start = re.search(r"computed\s*:\s*\{", script_content)
        if computed_start:
            # Find matching closing brace using bracket counting
            start_pos = computed_start.end() - 1  # Position of opening {
            brace_count = 0
            end_pos = start_pos
            for i in range(start_pos, len(script_content)):
                if script_content[i] == '{':
                    brace_count += 1
                elif script_content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i
                        break

            if end_pos > start_pos:
                computed_obj = script_content[start_pos + 1:end_pos]
                # Extract computed names - match identifiers before ( or :
                computed_names = re.findall(r"(\w+)\s*[:(]", computed_obj)
                computed.extend(computed_names)
        return computed

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
            if any(op in expression for op in ['++', '--', '+=', '-=', '*=', '/=', '%=', '&&', '||', '===', '!==', '==', '!=', '<=', '>=', '=', '+', '-', '*', '/', '%', '!', '<', '>']):
                continue

            # Filter out expressions with parentheses (function calls with args)
            if '(' in expression and ')' in expression:
                # Extract function name before parentheses
                func_match = re.match(r'(\w+)\s*\(', expression)
                if func_match:
                    handlers.append(func_match.group(1))
                continue

            # Filter out expressions with dots (property access like item.show = true)
            if '.' in expression:
                continue

            # Filter out $emit calls (these are emits, not method handlers)
            if expression.startswith('$emit'):
                continue

            # If it's a simple identifier (method name), capture it
            if re.match(r'^[a-zA-Z_$][\w$]*$', expression):
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
        self, style_info: dict[str, Any], parent_id: str
    ) -> list[Relationship]:
        """Parse the <style> section to extract v-bind() CSS variable linking.

        Vue 3.2+ allows binding JS variables directly in CSS:
        ```vue
        <style scoped>
        .text { color: v-bind(themeColor); }
        </style>
        ```

        This creates a REFERENCES relationship from component to the variable.
        """
        relationships = []
        style_content = style_info["content"]

        # Extract v-bind() calls
        css_bindings = self._extract_css_bindings(style_content)
        for var_name in css_bindings:
            # Try to determine the source (data, ref, computed, etc.)
            # Since we don't know the exact type, we create a generic reference
            # The relationship will link to whatever entity matches the variable name
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=f"{parent_id}::data::{var_name}",  # Fallback to data
                    kind=RelationshipKind.REFERENCES,
                    metadata={"binding_type": "css_variable"}
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
            if '-' in var_name:
                var_name = ''.join(word.capitalize() if i > 0 else word for i, word in enumerate(var_name.split('-')))
            bindings.append(var_name)
        return bindings

    def _extract_component_usage(self, template_content: str) -> list[str]:
        """Extract component usage from template.

        This is the MOST IMPORTANT relationship for building UI component trees.
        Detects components used in the template like <MyButton />, <my-button>, etc.

        Returns:
            List of component names in PascalCase
        """
        components = set()

        # Pattern 1: Self-closing components
        # Matches: <MyButton />, <MyButton/>, <my-button />
        self_closing_pattern = r"<([A-Z][a-zA-Z0-9]*)\s*[^>]*/>"
        for match in re.finditer(self_closing_pattern, template_content):
            components.add(match.group(1))

        # Pattern 2: Regular components (opening and closing tags)
        # Matches: <MyButton>...</MyButton>
        regular_pattern = r"<([A-Z][a-zA-Z0-9]*)\s*[^>]*>"
        for match in re.finditer(regular_pattern, template_content):
            component_name = match.group(1)
            # Skip native HTML elements (rough check - uppercase first letter suggests component)
            if component_name[0].isupper():
                components.add(component_name)

        # Pattern 3: Kebab-case components (Vue convention)
        # Matches: <my-button>, <user-profile>
        # Convert to PascalCase for consistency
        kebab_pattern = r"<([a-z]+(?:-[a-z]+)+)\s*[^>]*[/>]"
        for match in re.finditer(kebab_pattern, template_content):
            kebab_name = match.group(1)
            # Convert kebab-case to PascalCase
            pascal_name = ''.join(word.capitalize() for word in kebab_name.split('-'))
            components.add(pascal_name)

        return list(components)

    def _parse_script_content(
        self, script_text: str, file_path: Path, parent_id: str, source_lines: list[str]
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse script content using JavaScript parser (tree-sitter fallback)."""
        # This would use the JS parser directly if available
        # For now, fallback to regex-based extraction
        return [], []

    def _parse_template_content(self, node: Any, parent_id: str) -> list[Relationship]:
        """Parse template content using tree-sitter."""
        # Extract relationships from template AST
        return []
