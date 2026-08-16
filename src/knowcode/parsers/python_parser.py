"""Python parser using standard ast module.

The visitor is scope-aware:

* nested classes and functions receive qualified names built from their lexical
  parents (``Outer.inner``, ``Outer.method.local``);
* a call is extracted only from a definition's *own* body and never across a
  nested class/function/lambda boundary, so each entity owns its own calls;
* a bare-name call resolves to the nearest enclosing definition in the lexical
  scope chain and otherwise becomes an explicit scoped unresolved reference;
* a decorated definition spans its decorators, and decorator expressions are
  retained deterministically in source order; and
* module-level assignments (annotated, chained, and tuple-unpacked) become
  variable entities.

Inheritance targets keep their ``ref::`` form so :class:`GraphBuilder` can still
link them to local entities; imports point at the ``external`` namespace.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    dedupe_entities_by_id,
)


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


class PythonParser:
    """Parses Python source files into entities and relationships."""

    language_name = "python"

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse a Python file into entities and relationships."""
        file_path = Path(file_path)

        try:
            source_code = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Failed to read file: {e}"],
            )

        try:
            tree = ast.parse(source_code, filename=str(file_path))
        except SyntaxError as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Syntax error: {e}"],
            )

        source_lines = source_code.splitlines()
        module_name = file_path.stem

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        module_id = build_internal_entity_id(file_path, module_name)
        entities.append(
            Entity(
                id=module_id,
                kind=EntityKind.MODULE,
                name=module_name,
                qualified_name=module_name,
                location=Location(
                    file_path=str(file_path),
                    line_start=1,
                    line_end=len(source_lines) if source_lines else 1,
                ),
                docstring=ast.get_docstring(tree),
            )
        )

        # Imports are attributed to the module. Names are external symbols.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    relationships.append(
                        Relationship(
                            source_id=module_id,
                            target_id=build_external_reference_id(
                                self.language_name, alias.name
                            ),
                            kind=RelationshipKind.IMPORTS,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    relationships.append(
                        Relationship(
                            source_id=module_id,
                            target_id=build_external_reference_id(
                                self.language_name, node.module
                            ),
                            kind=RelationshipKind.IMPORTS,
                        )
                    )

        self._process_scope(
            body=tree.body,
            file_path=file_path,
            source_lines=source_lines,
            parent_id=module_id,
            parent_qname="",
            parent_is_class=False,
            is_module=True,
            scope_chain=[],
            entities=entities,
            relationships=relationships,
        )

        # Two declarations in one file cannot share one canonical ID. Dedupe the
        # real declarations only (the module entity at entities[0] is a synthetic
        # wrapper; a same-named top-level declaration is left to the graph merge).
        children, dedupe_errors = dedupe_entities_by_id(entities[1:])
        entities = [entities[0], *children]

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=dedupe_errors,
        )

    # ------------------------------------------------------------------
    # Scope recursion
    # ------------------------------------------------------------------

    def _process_scope(
        self,
        body: list[ast.stmt],
        file_path: Path,
        source_lines: list[str],
        parent_id: str,
        parent_qname: str,
        parent_is_class: bool,
        is_module: bool,
        scope_chain: list[dict[str, str]],
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        """Recursively extract child entities, containment, and scoped calls.

        ``parent_qname`` is empty at module level so top-level definitions get
        bare qualified names. The local symbol table is populated before any
        recursion so forward and sibling references resolve regardless of
        textual order.
        """
        local_symbols: dict[str, str] = {}
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_symbols[stmt.name] = self._qualified_name(parent_qname, stmt.name)
            elif is_module and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                for name in self._assignment_targets(stmt):
                    # Module-level variables use their bare name as qualified name.
                    local_symbols.setdefault(name, name)

        extended_chain = [*scope_chain, local_symbols]
        defined_variables: set[str] = set()

        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                self._define_class(
                    stmt,
                    file_path,
                    source_lines,
                    parent_id,
                    parent_qname,
                    extended_chain,
                    entities,
                    relationships,
                )
            elif isinstance(stmt, _FUNCTION_DEFS):
                kind = EntityKind.METHOD if parent_is_class else EntityKind.FUNCTION
                self._define_function(
                    stmt,
                    kind,
                    file_path,
                    source_lines,
                    parent_id,
                    parent_qname,
                    extended_chain,
                    entities,
                    relationships,
                )
            elif is_module and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                for name in self._assignment_targets(stmt):
                    if name in defined_variables:
                        continue
                    defined_variables.add(name)
                    self._define_module_variable(
                        stmt,
                        name,
                        file_path,
                        source_lines,
                        parent_id,
                        entities,
                        relationships,
                    )

        # Only function/method bodies own calls. Module and class bodies do not
        # emit CALLS edges, matching the prior parser's extraction surface.
        if not is_module and not parent_is_class:
            self._extract_scope_calls(
                body,
                caller_id=parent_id,
                caller_qname=parent_qname,
                scope_chain=extended_chain,
                file_path=file_path,
                relationships=relationships,
            )

    # ------------------------------------------------------------------
    # Definition builders
    # ------------------------------------------------------------------

    def _define_class(
        self,
        node: ast.ClassDef,
        file_path: Path,
        source_lines: list[str],
        parent_id: str,
        parent_qname: str,
        scope_chain: list[dict[str, str]],
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        qualified_name = self._qualified_name(parent_qname, node.name)
        class_id = build_internal_entity_id(file_path, qualified_name)
        start_line, end_line, decorators = self._definition_span(node)

        entities.append(
            Entity(
                id=class_id,
                kind=EntityKind.CLASS,
                name=node.name,
                qualified_name=qualified_name,
                location=Location(
                    file_path=str(file_path),
                    line_start=start_line,
                    line_end=end_line,
                    column_start=node.col_offset,
                    column_end=node.end_col_offset or 0,
                ),
                docstring=ast.get_docstring(node),
                source_code=self._source_lines(source_lines, start_line, end_line),
                metadata=self._decorator_metadata(decorators),
            )
        )
        relationships.append(
            Relationship(
                source_id=parent_id,
                target_id=class_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        for base in node.bases:
            base_name = self._get_name(base)
            if base_name:
                relationships.append(
                    Relationship(
                        source_id=class_id,
                        target_id=f"ref::{base_name}",
                        kind=RelationshipKind.INHERITS,
                    )
                )

        self._process_scope(
            body=node.body,
            file_path=file_path,
            source_lines=source_lines,
            parent_id=class_id,
            parent_qname=qualified_name,
            parent_is_class=True,
            is_module=False,
            scope_chain=scope_chain,
            entities=entities,
            relationships=relationships,
        )

    def _define_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        kind: EntityKind,
        file_path: Path,
        source_lines: list[str],
        parent_id: str,
        parent_qname: str,
        scope_chain: list[dict[str, str]],
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        qualified_name = self._qualified_name(parent_qname, node.name)
        func_id = build_internal_entity_id(file_path, qualified_name)
        start_line, end_line, decorators = self._definition_span(node)

        entities.append(
            Entity(
                id=func_id,
                kind=kind,
                name=node.name,
                qualified_name=qualified_name,
                location=Location(
                    file_path=str(file_path),
                    line_start=start_line,
                    line_end=end_line,
                    column_start=node.col_offset,
                    column_end=node.end_col_offset or 0,
                ),
                docstring=ast.get_docstring(node),
                signature=self._get_signature(node),
                source_code=self._source_lines(source_lines, start_line, end_line),
                metadata=self._decorator_metadata(decorators),
            )
        )
        relationships.append(
            Relationship(
                source_id=parent_id,
                target_id=func_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        self._process_scope(
            body=node.body,
            file_path=file_path,
            source_lines=source_lines,
            parent_id=func_id,
            parent_qname=qualified_name,
            parent_is_class=False,
            is_module=False,
            scope_chain=scope_chain,
            entities=entities,
            relationships=relationships,
        )

    def _define_module_variable(
        self,
        node: ast.Assign | ast.AnnAssign,
        name: str,
        file_path: Path,
        source_lines: list[str],
        parent_id: str,
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        start_line = node.lineno
        end_line = node.end_lineno or node.lineno
        var_id = build_internal_entity_id(file_path, name)

        entities.append(
            Entity(
                id=var_id,
                kind=EntityKind.VARIABLE,
                name=name,
                qualified_name=name,
                location=Location(
                    file_path=str(file_path),
                    line_start=start_line,
                    line_end=end_line,
                    column_start=node.col_offset,
                    column_end=node.end_col_offset or 0,
                ),
                source_code=self._source_lines(source_lines, start_line, end_line),
            )
        )
        relationships.append(
            Relationship(
                source_id=parent_id,
                target_id=var_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

    # ------------------------------------------------------------------
    # Call extraction and resolution
    # ------------------------------------------------------------------

    def _extract_scope_calls(
        self,
        body: list[ast.stmt],
        caller_id: str,
        caller_qname: str,
        scope_chain: list[dict[str, str]],
        file_path: Path,
        relationships: list[Relationship],
    ) -> None:
        """Extract CALLS relationships from a scope's own body only."""
        for stmt in body:
            if isinstance(stmt, _SCOPE_BOUNDARIES):
                # The nested definition owns its own calls; do not descend.
                continue
            for call_node in self._iter_own_calls(stmt):
                callee = self._get_call_name(call_node)
                if not callee:
                    continue
                relationships.append(
                    Relationship(
                        source_id=caller_id,
                        target_id=self._resolve_call(
                            callee, caller_qname, scope_chain, file_path
                        ),
                        kind=RelationshipKind.CALLS,
                    )
                )

    def _iter_own_calls(self, node: ast.AST) -> Iterator[ast.Call]:
        """Yield ``ast.Call`` nodes under ``node`` without crossing scope bounds."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            if isinstance(child, ast.Call):
                yield child
            yield from self._iter_own_calls(child)

    def _resolve_call(
        self,
        callee: str,
        caller_qname: str,
        scope_chain: list[dict[str, str]],
        file_path: Path,
    ) -> str:
        """Resolve a callee to an internal entity id or a scoped unresolved id."""
        # Attribute/method calls (self.run, obj.method, a.b.c) cannot be bound to
        # a local definition without dataflow; record them as scoped references.
        if "." not in callee:
            for symbols in reversed(scope_chain):
                if callee in symbols:
                    return build_internal_entity_id(file_path, symbols[callee])
        return build_unresolved_reference_id(
            self.language_name, file_path, caller_qname, callee
        )

    # ------------------------------------------------------------------
    # Source/model helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _qualified_name(parent_qname: str, name: str) -> str:
        return f"{parent_qname}.{name}" if parent_qname else name

    @staticmethod
    def _definition_span(
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[int, int, list[str]]:
        """Return (start_line, end_line, decorator_sources) for a definition.

        The span starts at the first decorator so locations and source snippets
        cover the complete, decorated declaration.
        """
        decorators = [ast.unparse(decorator) for decorator in node.decorator_list]
        if node.decorator_list:
            start_line = min(d.lineno for d in node.decorator_list)
        else:
            start_line = node.lineno
        end_line = node.end_lineno or node.lineno
        return start_line, end_line, decorators

    @staticmethod
    def _decorator_metadata(decorators: list[str]) -> dict[str, object]:
        if not decorators:
            return {}
        return {"decorators": decorators}

    @staticmethod
    def _source_lines(source_lines: list[str], start_line: int, end_line: int) -> str:
        return "\n".join(source_lines[start_line - 1 : end_line])

    @staticmethod
    def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> Iterator[str]:
        """Yield simple variable names bound by a module-level assignment."""
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from PythonParser._names_from_target(target)
        else:  # ast.AnnAssign
            yield from PythonParser._names_from_target(node.target)

    @staticmethod
    def _names_from_target(target: ast.expr) -> Iterator[str]:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from PythonParser._names_from_target(element)
        elif isinstance(target, ast.Starred):
            yield from PythonParser._names_from_target(target.value)
        # Attribute/Subscript targets are not simple module variables.

    def _get_call_name(self, node: ast.Call) -> str | None:
        """Get the name of a called function."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            value_name = self._get_name(node.func.value)
            if value_name:
                return f"{value_name}.{node.func.attr}"
            return node.func.attr
        return None

    def _get_name(self, node: ast.expr) -> str | None:
        """Get a dotted name from an expression node."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            value = self._get_name(node.value)
            if value:
                return f"{value}.{node.attr}"
            return node.attr
        return None

    @staticmethod
    def _get_signature(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> str:
        """Generate a function signature string."""
        args = node.args
        params: list[str] = []

        for arg in args.args:
            param = arg.arg
            if arg.annotation:
                param += f": {ast.unparse(arg.annotation)}"
            params.append(param)

        if args.vararg:
            param = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                param += f": {ast.unparse(args.vararg.annotation)}"
            params.append(param)

        if args.kwarg:
            param = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                param += f": {ast.unparse(args.kwarg.annotation)}"
            params.append(param)

        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{async_prefix}def {node.name}({', '.join(params)}){returns}"
