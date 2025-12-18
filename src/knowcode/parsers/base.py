"""Base parser using Tree-sitter."""

from __future__ import annotations
from pathlib import Path
from typing import Any, Optional

from tree_sitter import Parser
import tree_sitter_languages

from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
)


class TreeSitterParser:
    """Base class for parsers using Tree-sitter."""

    def __init__(self, language_name: str) -> None:
        """Initialize parser for a specific language.

        Args:
            language_name: Name of the language (e.g., 'python', 'javascript', 'java').
        """
        self.language_name = language_name
        self.language = tree_sitter_languages.get_language(language_name)
        self.parser = Parser()
        self.parser.set_language(self.language)

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse a source file.

        Args:
            file_path: Path to the source file.

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
            tree = self.parser.parse(bytes(source_code, "utf8"))
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Parse error: {e}"],
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
        )
        entities.append(module_entity)

        # Delegate to language-specific extraction
        child_entities, child_rels = self._extract_entities(
            tree.root_node, file_path, module_id, source_code, source_lines
        )
        entities.extend(child_entities)
        relationships.extend(child_rels)

        # Handle errors from tree-sitter
        if tree.root_node.has_error:
             # We might want to be more specific here, but for now just flag it
             # Don't fail completely, as partial AST is often useful
             errors.append("Tree-sitter reported syntax errors in file")

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
        """Extract entities from the AST. Must be implemented by subclasses.
        
        Args:
            node: Current AST node to process.
            file_path: Path to the file being parsed.
            parent_id: ID of the parent entity (for containment).
            source_code: Full source code text.
            source_lines: Source code split by lines.
            
        Returns:
            Tuple of (entities list, relationships list).
        """
        raise NotImplementedError


    def _get_text(self, node: Any, source_bytes: bytes) -> str:
        """Get text content of a node."""
        return node.text.decode("utf8")

    def _get_location(self, node: Any, file_path: Path) -> Location:
        """Get location object for a node."""
        return Location(
            file_path=str(file_path),
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            column_start=node.start_point[1],
            column_end=node.end_point[1],
        )

    def _create_entity(
        self,
        node: Any,
        kind: EntityKind,
        name: str,
        qualified_name: str,
        file_path: Path,
        source_lines: list[str],
        docstring: Optional[str] = None,
        signature: Optional[str] = None,
    ) -> Entity:
        """Helper to create an entity."""
        # Extract source code for the node
        start_line = node.start_point[0]
        end_line = node.end_point[0] + 1
        node_source = "\n".join(source_lines[start_line:end_line])

        return Entity(
            id=f"{file_path}::{qualified_name}",
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            location=self._get_location(node, file_path),
            docstring=docstring,
            signature=signature,
            source_code=node_source,
        )
