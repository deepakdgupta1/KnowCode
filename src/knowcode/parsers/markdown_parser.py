"""Markdown document parser."""

from __future__ import annotations

import re
from pathlib import Path

from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)


class MarkdownParser:
    """Parses Markdown files into entities based on heading structure."""

    # Regex patterns
    HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse a Markdown file.

        Args:
            file_path: Path to the Markdown file.

        Returns:
            ParseResult with document and section entities.
        """
        file_path = Path(file_path)
        errors: list[str] = []

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Failed to read file: {e}"],
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        lines = content.splitlines()

        # Create document entity
        doc_name = file_path.stem
        doc_id = f"{file_path}::{doc_name}"

        # Extract first paragraph as description (skip headings)
        description = self._extract_description(content)

        doc_entity = Entity(
            id=doc_id,
            kind=EntityKind.DOCUMENT,
            name=doc_name,
            qualified_name=doc_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(lines),
            ),
            docstring=description,
        )
        entities.append(doc_entity)

        # Remove code blocks to avoid matching headings inside them
        content_no_code = self.CODE_BLOCK_PATTERN.sub("", content)

        # Extract headings
        headings = self._extract_headings(content_no_code, lines)

        # Build section hierarchy
        section_stack: list[tuple[int, str]] = [(0, doc_id)]  # (level, entity_id)

        for heading in headings:
            level, title, line_num = heading
            section_id = f"{file_path}::{self._slugify(title)}"

            # Find line range for this section
            line_end = self._find_section_end(line_num, headings, len(lines))

            section_entity = Entity(
                id=section_id,
                kind=EntityKind.SECTION,
                name=title,
                qualified_name=title,
                location=Location(
                    file_path=str(file_path),
                    line_start=line_num,
                    line_end=line_end,
                ),
                metadata={"level": str(level)},
            )
            entities.append(section_entity)

            # Find parent (closest heading with lower level)
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()

            parent_id = section_stack[-1][1] if section_stack else doc_id

            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=section_id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

            section_stack.append((level, section_id))

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )

    def _extract_headings(
        self, content: str, lines: list[str]
    ) -> list[tuple[int, str, int]]:
        """Extract headings with their levels and line numbers.

        Returns:
            List of (level, title, line_number) tuples.
        """
        headings: list[tuple[int, str, int]] = []

        for i, line in enumerate(lines, start=1):
            match = self.HEADING_PATTERN.match(line)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()
                headings.append((level, title, i))

        return headings

    def _find_section_end(
        self,
        start_line: int,
        headings: list[tuple[int, str, int]],
        total_lines: int,
    ) -> int:
        """Find the end line of a section."""
        current_level = None
        for level, _, line_num in headings:
            if line_num == start_line:
                current_level = level
            elif current_level is not None and line_num > start_line:
                if level <= current_level:
                    return line_num - 1

        return total_lines

    def _extract_description(self, content: str) -> str:
        """Extract first paragraph as document description."""
        # Remove code blocks
        content = self.CODE_BLOCK_PATTERN.sub("", content)

        # Split into paragraphs
        paragraphs = re.split(r"\n\s*\n", content)

        for para in paragraphs:
            para = para.strip()
            # Skip headings and empty paragraphs
            if para and not para.startswith("#"):
                # Take first 500 chars max
                return para[:500]

        return ""

    def _slugify(self, text: str) -> str:
        """Convert text to a URL-friendly slug."""
        # Lowercase and replace spaces with hyphens
        slug = text.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        return slug.strip("-")
