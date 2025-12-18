"""Python parser using standard ast module."""

from __future__ import annotations


import ast
from pathlib import Path
from typing import Optional

from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)


class PythonParser:
    """Parses Python source files into entities and relationships."""

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse a Python file.

        Args:
            file_path: Path to the Python file.

        Returns:
            ParseResult with entities and relationships.
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

        try:
            tree = ast.parse(source_code, filename=str(file_path))
        except SyntaxError as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Syntax error: {e}"],
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        source_lines = source_code.splitlines()

        # Create module entity
        module_name = file_path.stem
        module_id = f"{file_path}::{module_name}"
        module_entity = Entity(
            id=module_id,
            kind=EntityKind.MODULE,
            name=module_name,
            qualified_name=module_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(source_lines),
            ),
            docstring=ast.get_docstring(tree),
        )
        entities.append(module_entity)

        # Extract imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    relationships.append(
                        Relationship(
                            source_id=module_id,
                            target_id=f"external::{alias.name}",
                            kind=RelationshipKind.IMPORTS,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    relationships.append(
                        Relationship(
                            source_id=module_id,
                            target_id=f"external::{node.module}",
                            kind=RelationshipKind.IMPORTS,
                        )
                    )

        # Process top-level definitions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_entities, class_rels = self._parse_class(
                    node, file_path, module_id, source_lines
                )
                entities.extend(class_entities)
                relationships.extend(class_rels)

            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                func_entity, func_rels = self._parse_function(
                    node, file_path, module_id, source_lines
                )
                entities.append(func_entity)
                relationships.extend(func_rels)

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )

    def _parse_class(
        self,
        node: ast.ClassDef,
        file_path: Path,
        parent_id: str,
        source_lines: list[str],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Parse a class definition."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        class_id = f"{file_path}::{node.name}"

        # Get source code for the class
        source_code = self._get_source(node, source_lines)

        class_entity = Entity(
            id=class_id,
            kind=EntityKind.CLASS,
            name=node.name,
            qualified_name=node.name,
            location=Location(
                file_path=str(file_path),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                column_start=node.col_offset,
                column_end=node.end_col_offset or 0,
            ),
            docstring=ast.get_docstring(node),
            source_code=source_code,
        )
        entities.append(class_entity)

        # Add contains relationship
        relationships.append(
            Relationship(
                source_id=parent_id,
                target_id=class_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        # Add inheritance relationships
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

        # Parse methods
        for child in node.body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                method_entity, method_rels = self._parse_method(
                    child, file_path, class_id, node.name, source_lines
                )
                entities.append(method_entity)
                relationships.extend(method_rels)

        return entities, relationships

    def _parse_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: Path,
        parent_id: str,
        source_lines: list[str],
    ) -> tuple[Entity, list[Relationship]]:
        """Parse a function definition."""
        relationships: list[Relationship] = []

        func_id = f"{file_path}::{node.name}"
        source_code = self._get_source(node, source_lines)
        signature = self._get_signature(node)

        entity = Entity(
            id=func_id,
            kind=EntityKind.FUNCTION,
            name=node.name,
            qualified_name=node.name,
            location=Location(
                file_path=str(file_path),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                column_start=node.col_offset,
                column_end=node.end_col_offset or 0,
            ),
            docstring=ast.get_docstring(node),
            signature=signature,
            source_code=source_code,
        )

        # Add contains relationship
        relationships.append(
            Relationship(
                source_id=parent_id,
                target_id=func_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        # Find calls within the function
        call_rels = self._extract_calls(node, func_id)
        relationships.extend(call_rels)

        return entity, relationships

    def _parse_method(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: Path,
        class_id: str,
        class_name: str,
        source_lines: list[str],
    ) -> tuple[Entity, list[Relationship]]:
        """Parse a method definition."""
        relationships: list[Relationship] = []

        method_id = f"{file_path}::{class_name}.{node.name}"
        source_code = self._get_source(node, source_lines)
        signature = self._get_signature(node)

        entity = Entity(
            id=method_id,
            kind=EntityKind.METHOD,
            name=node.name,
            qualified_name=f"{class_name}.{node.name}",
            location=Location(
                file_path=str(file_path),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                column_start=node.col_offset,
                column_end=node.end_col_offset or 0,
            ),
            docstring=ast.get_docstring(node),
            signature=signature,
            source_code=source_code,
        )

        # Add contains relationship
        relationships.append(
            Relationship(
                source_id=class_id,
                target_id=method_id,
                kind=RelationshipKind.CONTAINS,
            )
        )

        # Find calls within the method
        call_rels = self._extract_calls(node, method_id)
        relationships.extend(call_rels)

        return entity, relationships

    def _extract_calls(
        self,
        node: ast.AST,
        caller_id: str,
    ) -> list[Relationship]:
        """Extract function/method calls from a node."""
        relationships: list[Relationship] = []

        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                callee_name = self._get_call_name(child)
                if callee_name:
                    relationships.append(
                        Relationship(
                            source_id=caller_id,
                            target_id=f"ref::{callee_name}",
                            kind=RelationshipKind.CALLS,
                        )
                    )

        return relationships

    def _get_call_name(self, node: ast.Call) -> Optional[str]:
        """Get the name of a called function."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            value_name = self._get_name(node.func.value)
            if value_name:
                return f"{value_name}.{node.func.attr}"
            return node.func.attr
        return None

    def _get_name(self, node: ast.expr) -> Optional[str]:
        """Get name from an expression node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            value = self._get_name(node.value)
            if value:
                return f"{value}.{node.attr}"
            return node.attr
        return None

    def _get_source(self, node: ast.AST, source_lines: list[str]) -> str:
        """Get source code for a node."""
        if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
            return ""
        start = node.lineno - 1
        end = node.end_lineno if node.end_lineno else node.lineno
        return "\n".join(source_lines[start:end])

    def _get_signature(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> str:
        """Generate function signature string."""
        args = node.args
        params: list[str] = []

        # Positional args
        for arg in args.args:
            param = arg.arg
            if arg.annotation:
                param += f": {ast.unparse(arg.annotation)}"
            params.append(param)

        # *args
        if args.vararg:
            param = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                param += f": {ast.unparse(args.vararg.annotation)}"
            params.append(param)

        # **kwargs
        if args.kwarg:
            param = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                param += f": {ast.unparse(args.kwarg.annotation)}"
            params.append(param)

        # Return type
        returns = ""
        if node.returns:
            returns = f" -> {ast.unparse(node.returns)}"

        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{async_prefix}def {node.name}({', '.join(params)}){returns}"
